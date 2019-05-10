"""
Wavelength Calibration
by comparison to a reference spectrum
Loosely bases on the IDL wavecal function
"""

import datetime as dt
import logging

import astropy.io.fits as fits
import matplotlib.pyplot as plt
import numpy as np
from numpy.polynomial.polynomial import polyval2d
from scipy import signal
from scipy.constants import speed_of_light
from scipy.optimize import curve_fit, least_squares

from . import util
from .instruments import instrument_info


class AlignmentPlot:
    """
    Makes a plot which can be clicked to align the two spectra, reference and observed
    """

    def __init__(self, ax, obs, lines, offset=[0, 0]):
        self.im = ax
        self.first = True
        self.nord, self.ncol = obs.shape
        self.RED, self.GREEN, self.BLUE = 0, 1, 2

        self.obs = obs
        self.lines = lines

        self.order_first = 0
        self.spec_first = ""
        self.x_first = 0
        self.offset = offset

        self.make_ref_image()

    def make_ref_image(self):
        """ create and show the reference plot, with the two spectra """
        ref_image = np.zeros((self.nord * 2, self.ncol, 3))
        for iord in range(self.nord):
            ref_image[iord * 2, :, self.RED] = 10 * np.ma.filled(self.obs[iord], 0)
            if 0 <= iord + self.offset[0] < self.nord:
                for line in self.lines[self.lines["order"] == iord]:
                    first = np.clip(line["xfirst"] + self.offset[1], 0, self.ncol)
                    last = np.clip(line["xlast"] + self.offset[1], 0, self.ncol)
                    ref_image[
                        (iord + self.offset[0]) * 2 + 1, first:last, self.GREEN
                    ] = (
                        10
                        * line["height"]
                        * signal.gaussian(last - first, line["width"])
                    )
        ref_image = np.clip(ref_image, 0, 1)
        ref_image[ref_image < 0.1] = 0

        self.im.imshow(
            ref_image,
            aspect="auto",
            origin="lower",
            extent=(0, self.ncol, 0, self.nord),
        )
        self.im.figure.suptitle(
            "Alignment, Observed: RED, Reference: GREEN\nGreen should be above red!"
        )
        self.im.axes.set_xlabel("x [pixel]")
        self.im.axes.set_ylabel("Order")

        self.im.figure.canvas.draw()

    def connect(self):
        """ connect the click event with the appropiate function """
        self.cidclick = self.im.figure.canvas.mpl_connect(
            "button_press_event", self.on_click
        )

    def on_click(self, event):
        """ On click offset the reference by the distance between click positions """
        if event.ydata is None:
            return
        order = int(np.floor(event.ydata))
        spec = "ref" if (event.ydata - order) > 0.5 else "obs"  # if True then reference
        x = event.xdata
        print("Order: %i, Spectrum: %s, x: %g" % (order, "ref" if spec else "obs", x))

        # on every second click
        if self.first:
            self.first = False
            self.order_first = order
            self.spec_first = spec
            self.x_first = x
        else:
            # Clicked different spectra
            if spec != self.spec_first:
                self.first = True
                direction = -1 if spec == "ref" else 1
                offset_orders = int(order - self.order_first) * direction
                offset_x = int(x - self.x_first) * direction
                self.offset[0] += offset_orders
                self.offset[1] += offset_x
                self.make_ref_image()


class WavelengthCalibration:
    """
    Wavelength Calibration Module

    Takes an observed wavelength image and the reference linelist
    and returns the wavelength at each pixel
    """
    def __init__(
        self,
        threshold=100,
        degree=(6, 6),
        iterations=3,
        mode="2D",
        shift_window=0.01,
        manual=False,
        polarim=False,
        plot=True,
    ):
        #:float: Residual threshold in m/s above which to remove lines
        self.threshold = threshold
        if np.isscalar(degree):
            degree = (degree, degree)
        #:tuple(int, int): polynomial degree of the wavelength fit in (pixel, order) direction
        self.degree = degree
        #:int: Number of iterations in the remove residuals, auto id, loop
        self.iterations = iterations
        #:{"1D", "2D"}: Wether to use 1D or 2D polynomials for the wavelength solution
        self.mode = mode
        #:float: Fraction if the number of columns to use in the alignment of individual orders. Set to 0 to disable
        self.shift_window = shift_window
        #:bool: Wether to manually align the reference instead of using cross correlation
        self.manual = manual
        #:bool: Wether to use polarimetric orders instead of the usual ones. I.e. Each pair of two orders represents the same data. Not Supported yet
        self.polarim = polarim
        #:int: Wether to plot the results. Set to 2 to plot during all steps.
        self.plot = plot

        #:int: Number of orders in the observation
        self.nord = None
        #:int: Number of columns in the observation
        self.ncol = None

    def normalize(self, obs, lines):
        """
        Normalize the observation and reference list in each order individually
        Copies the data if the image, but not of the linelist

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed image
        lines : recarray of shape (nlines,)
            reference linelist

        Returns
        -------
        obs : array of shape (nord, ncol)
            normalized image
        lines : recarray of shape (nlines,)
            normalized reference linelist
        """
        # normalize order by order
        obs = np.ma.copy(obs)
        for i in range(len(obs)):
            obs[i] -= np.ma.min(obs[i][obs[i] > 0])
            obs[i] /= np.ma.max(obs[i])
        obs[obs <= 0] = np.ma.masked

        # Normalize lines in each order
        for order in np.unique(lines["order"]):
            select = lines["order"] == order
            topheight = np.max(lines[select]["height"])
            lines["height"][select] /= topheight

        return obs, lines

    def create_image_from_lines(self, lines):
        """
        Create a reference image based on a line list
        Each line will be approximated by a Gaussian
        Space inbetween lines is 0
        The number of orders is from 0 to the maximum order

        Parameters
        ----------
        lines : recarray of shape (nlines,)
            line data

        Returns
        -------
        img : array of shape (nord, ncol)
            New reference image
        """
        min_order = np.min(lines["order"])
        max_order = np.max(lines["order"])
        img = np.zeros((max_order - min_order + 1, self.ncol))
        for line in lines:
            if line["order"] < 0:
                continue
            if line["xlast"] < 0 or line["xfirst"] > self.ncol:
                continue
            first = max(line["xfirst"], 0)
            last = min(line["xlast"], self.ncol)
            img[line["order"] - min_order, first:last] = line[
                "height"
            ] * signal.gaussian(last - first, line["width"])
        return img

    def align_manual(self, obs, lines):
        """
        Open an AlignmentPlot window for manual selection of the alignment

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed image
        lines : recarray of shape (nlines,)
            reference linelist

        Returns
        -------
        offset : tuple(int, int)
            offset in order and column to be applied to each line in the linelist
        """
        _, ax = plt.subplots()
        ap = AlignmentPlot(ax, obs, lines)
        ap.connect()
        plt.show()
        offset = ap.offset
        return offset

    def apply_alignment_offset(self, lines, offset, select=None):
        """
        Apply an offset to the linelist

        Parameters
        ----------
        lines : recarray of shape (nlines,)
            reference linelist
        offset : tuple(int, int)
            offset in (order, column)
        select : array of shape(nlines,), optional
            Mask that defines which lines the offset applies to

        Returns
        -------
        lines : recarray of shape (nlines,)
            linelist with offset applied
        """
        if select is None:
            select = slice(None)
        lines["xfirst"][select] += offset[1]
        lines["xlast"][select] += offset[1]
        lines["posm"][select] += offset[1]
        lines["order"][select] += offset[0]
        return lines

    def align(self, obs, lines):
        """
        Align the observation with the reference spectrum
        Either automatically using cross correlation or manually (visually)

        Parameters
        ----------
        obs : array[nrow, ncol]
            observed wavelength calibration spectrum (e.g. obs=ThoriumArgon)
        lines : struct_array
            reference line data
        manual : bool, optional
            wether to manually align the spectra (default: False)
        plot : bool, optional
            wether to plot the alignment (default: False)

        Returns
        -------
        offset: tuple(int, int)
            offset in order and column
        """
        obs = np.ma.filled(obs, 0)

        if not self.manual:
            # make image from lines
            img = self.create_image_from_lines(lines)

            # Cross correlate with obs image
            # And determine overall offset
            correlation = signal.correlate2d(obs, img, mode="same")
            offset_order, offset_x = np.unravel_index(
                np.argmax(correlation), correlation.shape
            )

            offset_order = offset_order - img.shape[0] / 2 + 1
            offset_x = offset_x - img.shape[1] / 2 + 1
            offset = [int(offset_order), int(offset_x)]

            # apply offset
            lines = self.apply_alignment_offset(lines, offset)

            if self.shift_window != 0:
                # Shift individual orders to fit reference
                # Only allow a small shift here (1%) ?
                img = self.create_image_from_lines(lines)
                for i in range(max(offset[0], 0), min(len(obs), len(img))):
                    correlation = signal.correlate(obs[i], img[i], mode="same")
                    width = int(self.ncol * self.shift_window) // 2
                    low, high = self.ncol // 2 - width, self.ncol // 2 + width
                    offset_x = np.argmax(correlation[low:high]) + low
                    offset_x = int(offset_x - self.ncol / 2 + 1)

                    select = lines["order"] == i
                    lines = self.apply_alignment_offset(lines, (0, offset_x), select)

        if self.plot or self.manual:
            offset = self.align_manual(obs, lines)
            lines = self.apply_alignment_offset(lines, offset)

        logging.debug(f"Offset order: {offset[0]}, Offset pixel: {offset[1]}")

        return lines

    def fit_lines(self, obs, lines):
        """
        Determine exact position of each line on the detector based on initial guess

        This fits a Gaussian to each line, and uses the peak position as a new solution

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed wavelength calibration image
        lines : recarray of shape (nlines,)
            reference line data

        Returns
        -------
        lines : recarray of shape (nlines,)
            Updated line information (posm is changed)
        """
        # For each line fit a gaussian to the observation
        for i, line in enumerate(lines):
            if line["posm"] < 0 or line["posm"] >= obs.shape[1]:
                # Line outside pixel range
                continue
            if line["order"] < 0 or line["order"] >= len(obs):
                # Line outside order range
                continue
            low = int(line["posm"] - line["width"] * 5)
            low = max(low, 0)
            high = int(line["posm"] + line["width"] * 5)
            high = min(high, len(obs[line["order"]]))

            section = obs[line["order"], low:high]
            x = np.arange(low, high, 1)
            x = np.ma.masked_array(x, mask=np.ma.getmaskarray(section))

            try:
                coef = util.gaussfit2(x, section)
                lines[i]["posm"] = coef[1]
            except:
                # Gaussian fit failed, dont use line
                lines[i]["flag"] = False

            if self.plot >= 2:
                x2 = np.linspace(x.min(), x.max(), len(x) * 100)
                plt.plot(x, section, label="Observation")
                plt.plot(x2, util.gaussval2(x2, *coef), label="Fit")
                plt.title("Gaussian Fit to spectral line")
                plt.xlabel("x [pixel]")
                plt.ylabel("Intensity [a.u.]")
                plt.legend()
                plt.show()

        return lines

    def build_2d_solution(self, lines, plot=False):
        """
        Create a 2D polynomial fit to flagged lines

        Parameters
        ----------
        lines : struc_array
            line data
        degree : tuple(int, int), optional
            polynomial degree of the fit in (column, order) dimension (default: (6, 6))
        plot : bool, optional
            wether to plot the solution (default: False)

        Returns
        -------
        coef : array[degree_x, degree_y]
            2d polynomial coefficients
        """

        # Only use flagged data
        mask = lines["flag"]  # True: use line, False: dont use line
        m_wave = lines["wll"][mask]
        m_pix = lines["posm"][mask]
        m_ord = lines["order"][mask]

        if self.mode == "1D":
            nord = m_ord.max() + 1
            coef = np.zeros((nord, self.degree[0] + 1))
            for i in range(nord):
                select = m_ord == i
                coef[i] = np.polyfit(m_pix[select], m_wave[select], deg=self.degree[0])
        elif self.mode == "2D":
            # 2d polynomial fit with: x = column, y = order, z = wavelength
            coef = util.polyfit2d(m_pix, m_ord, m_wave, degree=self.degree, plot=False)
        else:
            raise ValueError(
                f"Parameter 'mode' not understood. Expected '1D' or '2D' but got {mode}"
            )

        if plot or self.plot >= 2:
            orders = np.unique(lines["order"])
            norders = len(orders)
            for i, order in enumerate(orders):
                plt.subplot(int(np.ceil(norders / 2)), 2, i + 1)
                order_lines = lines[mask][lines[mask]["order"] == order]
                if len(order_lines) > 0:
                    residual = self.calculate_residual(coef, order_lines)
                    plt.plot(order_lines["posm"], residual, "rx")
                    plt.hlines(
                        [0], order_lines["posm"].min(), order_lines["posm"].max()
                    )
            plt.show()

        return coef

    def evaluate_solution(self, pos, order, solution):
        """
        Evaluate the 1d or 2d wavelength solution at the given pixel positions and orders

        Parameters
        ----------
        pos : array
            pixel position on the detector (i.e. x axis)
        order : array
            order of each point
        solution : array of shape (nord, ndegree) or (degree_x, degree_y)
            polynomial coefficients. For mode=1D, one set of coefficients per order.
            For mode=2D, the first dimension is for the positions and the second for the orders
        mode : str, optional
            Wether to interpret the solution as 1D or 2D polynomials, by default "1D"

        Returns
        -------
        result: array
            Evaluated polynomial

        Raises
        ------
        ValueError
            If pos and order have different shapes, or mode is of the wrong value
        """
        if not np.array_equal(pos.shape, order.shape):
            raise ValueError("pos and order must have the same shape")
        if self.mode == "1D":
            result = np.zeros(pos.shape)
            for i in np.unique(order):
                select = order == i
                result[select] = np.polyval(solution[i], pos[select])
        elif self.mode == "2D":
            result = np.polynomial.polynomial.polyval2d(pos, order, solution)
        else:
            raise ValueError(
                f"Parameter 'mode' not understood, expected '1D' or '2D' but got {mode}"
            )
        return result

    def make_wave(self, obs, wave_solution, plot=False):
        """Expand polynomial wavelength solution into full image

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed wavelength spectrum
        wave_solution : array of shape(degree,)
            polynomial coefficients of wavelength solution
        plot : bool, optional
            wether to plot the solution (default: False)

        Returns
        -------
        wave_img : array of shape (nord, ncol)
            wavelength solution for each point in the spectrum
        """

        y, x = np.indices((self.nord, self.ncol))
        wave_img = self.evaluate_solution(x, y, wave_solution)

        if plot or self.plot >= 2:
            plt.subplot(211)
            plt.title(
                "Wavelength solution with Wavelength calibration spectrum\nOrders are in different colours"
            )
            plt.xlabel("Wavelength")
            plt.ylabel("Observed spectrum")
            for i in range(self.nord):
                plt.plot(wave_img[i], obs[i], label="Order %i" % i)

            plt.subplot(212)
            plt.title("2D Wavelength solution")
            plt.imshow(
                wave_img,
                aspect="auto",
                origin="lower",
                extent=(0, self.ncol, 0, self.nord),
            )
            cbar = plt.colorbar()
            plt.xlabel("Column")
            plt.ylabel("Order")
            cbar.set_label("Wavelength [Å]")
            plt.show()

        return wave_img

    def auto_id(self, obs, wave_img, lines):
        """Automatically identify peaks that are close to known lines 

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed spectrum
        wave_img : array of shape (nord, ncol)
            wavelength solution image
        lines : struc_array
            line data
        threshold : int, optional
            difference threshold between line positions in m/s, until which a line is considered identified (default: 1)
        plot : bool, optional
            wether to plot the new lines

        Returns
        -------
        lines : struct_array
            line data with new flags
        """

        # Option 1:
        # Step 1: Loop over unused lines in lines
        # Step 2: find peaks in neighbourhood
        # Step 3: Toggle flag on if close
        counter = 0
        for i, line in enumerate(lines):
            if line["flag"]:
                # Line is already in use
                continue
            if line["order"] < 0 or line["order"] >= self.nord:
                # Line outside order range
                continue
            iord = line["order"]
            if line["wll"] < wave_img[iord][0] or line["posm"] >= wave_img[iord][-1]:
                # Line outside pixel range
                continue

            wl = line["wll"]
            width = line["width"] * 10
            wave = wave_img[iord]
            order_obs = obs[iord]
            # Find where the line should be
            try:
                idx = np.digitize(wl, wave)
            except ValueError:
                # Wavelength solution is not monotonic
                idx = np.where(wave >= wl)[0][0]

            low = int(idx - width)
            low = max(low, 0)
            high = int(idx + width)
            high = min(high, len(order_obs))

            vec = order_obs[low:high]
            if np.all(np.ma.getmaskarray(vec)):
                continue
            # Find the best fitting peak
            # TODO use gaussian fit?
            peak_idx, _ = signal.find_peaks(vec, height=np.ma.median(vec))
            if len(peak_idx) > 0:
                pos_wave = wave[low:high][peak_idx]
                residual = np.abs(wl - pos_wave) / wl * speed_of_light
                idx = np.argmin(residual)
                if residual[idx] < self.threshold:
                    counter += 1
                    lines["flag"][i] = True
                    lines["posm"][i] = low + peak_idx[idx]

        logging.info("AutoID identified %i new lines", counter)

        return lines

    def calculate_residual(self, wave_solution, lines):
        """
        Calculate all residuals of all given lines

        Residual = (Wavelength Solution - Expected Wavelength) / Expected Wavelength * speed of light

        Parameters
        ----------
        wave_solution : array of shape (degree_x, degree_y)
            polynomial coefficients of the wavelength solution (in numpy format)
        lines : recarray of shape (nlines,)
            contains the position of the line on the detector (posm), the order (order), and the expected wavelength (wll)

        Returns
        -------
        residual : array of shape (nlines,)
            Residual of each line in m/s
        """
        x = lines["posm"]
        y = lines["order"]
        mask = ~lines["flag"]

        solution = self.evaluate_solution(x, y, wave_solution)

        residual = (solution - lines["wll"]) / lines["wll"] * speed_of_light
        residual = np.ma.masked_array(residual, mask=mask)
        return residual

    def reject_outlier(self, residual, lines):
        """
        Reject the strongest outlier

        Parameters
        ----------
        residual : array of shape (nlines,)
            residuals of all lines
        lines : recarray of shape (nlines,)
            line data

        Returns
        -------
        lines : struct_array
            line data with one more flagged line
        residual : array of shape (nlines,)
            residuals of each line, with outliers masked (including the new one)
        """

        # Strongest outlier
        ibad = np.ma.argmax(np.abs(residual))
        lines["flag"][ibad] = False

        return lines

    def reject_lines(self, lines, plot=False):
        """
        Reject the largest outlier one by one until all residuals are lower than the threshold

        Parameters
        ----------
        lines : recarray of shape (nlines,)
            Line data with pixel position, and expected wavelength
        threshold : float, optional
            upper limit for the residual, by default 100
        degree : tuple, optional
            polynomial degree of the wavelength solution (pixel, column) (default: (6, 6))
        plot : bool, optional
            Wether to plot the results (default: False)

        Returns
        -------
        lines : recarray of shape (nlines,)
            Line data with updated flags
        """

        wave_solution = self.build_2d_solution(lines)
        residual = self.calculate_residual(wave_solution, lines)
        nbad = 0
        while np.ma.any(np.abs(residual) > self.threshold):
            lines = self.reject_outlier(residual, lines)
            wave_solution = self.build_2d_solution(lines)
            residual = self.calculate_residual(wave_solution, lines)
            nbad += 1
        logging.info("Discarding %i lines", nbad)

        if plot or self.plot >= 2:
            mask = lines["flag"]
            _, axis = plt.subplots()
            axis.plot(lines["order"][mask], residual[mask], "+", label="Accepted Lines")
            axis.plot(
                lines["order"][~mask], residual[~mask], "d", label="Rejected Lines"
            )
            axis.set_xlabel("Order")
            axis.set_ylabel("Residual [m/s]")
            axis.set_title("Residuals versus order")
            axis.legend()

            fig, ax = plt.subplots(
                nrows=self.nord // 2, ncols=2, sharex=True, squeeze=False
            )
            plt.subplots_adjust(hspace=0)
            fig.suptitle("Residuals of each order versus image columns")

            for iord in range(self.nord):
                order_lines = lines[lines["order"] == iord]
                solution = self.evaluate_solution(
                    order_lines["posm"], order_lines["order"], wave_solution
                )
                # Residual in m/s
                residual = (
                    (solution - order_lines["wll"])
                    / order_lines["wll"]
                    * speed_of_light
                )
                mask = order_lines["flag"]
                ax[iord // 2, iord % 2].plot(
                    order_lines["posm"][mask],
                    residual[mask],
                    "+",
                    label="Accepted Lines",
                )
                ax[iord // 2, iord % 2].plot(
                    order_lines["posm"][~mask],
                    residual[~mask],
                    "d",
                    label="Rejected Lines",
                )
                # ax[iord // 2, iord % 2].tick_params(labelleft=False)
                ax[iord // 2, iord % 2].set_ylim(
                    -self.threshold * 1.5, +self.threshold * 1.5
                )

            ax[-1, 0].set_xlabel("x [pixel]")
            ax[-1, 1].set_xlabel("x [pixel]")

            ax[0, 0].legend()

            plt.show()
        return lines

    def execute(self, obs, lines):
        """
        Perform the whole wavelength calibration procedure with the current settings

        Parameters
        ----------
        obs : array of shape (nord, ncol)
            observed image
        lines : recarray of shape (nlines,)
            reference linelist

        Returns
        -------
        wave_img : array of shape (nord, ncol)
            Wavelength solution for each pixel

        Raises
        ------
        NotImplementedError
            If polarimitry flag is set
        """
        if self.polarim:
            raise NotImplementedError("polarized orders not implemented yet")

        self.nord, self.ncol = obs.shape
        obs, lines = self.normalize(obs, lines)

        # Step 1: align obs and reference
        lines = self.align(obs, lines)

        # Step 2: Locate the lines on the detector, and update the pixel position
        lines = self.fit_lines(obs, lines)

        for i in range(self.iterations):
            logging.info(f"Wavelength calibration iteration: {i}")
            # Step 3: Create a wavelength solution on known lines
            wave_solution = self.build_2d_solution(lines)
            wave_img = self.make_wave(obs, wave_solution)
            # Step 4: Identify lines that fit into the solution
            lines = self.auto_id(obs, wave_img, lines)
            # Step 5: Reject outliers
            lines = self.reject_lines(lines)

        logging.info(
            "Number of lines used for wavelength calibration: %i",
            np.count_nonzero(lines["flag"]),
        )

        # Step 6: build final 2d solution
        wave_solution = self.build_2d_solution(lines, plot=self.plot)
        wave_img = self.make_wave(obs, wave_solution, plot=self.plot)

        return wave_img
