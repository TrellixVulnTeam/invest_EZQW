"""InVEST Habitat Quality model."""
from __future__ import absolute_import
import collections
import os
import logging

import numpy
from osgeo import gdal
from osgeo import osr
import pygeoprocessing

from . import utils
from . import validation

LOGGER = logging.getLogger('natcap.invest.habitat_quality')

_OUT_NODATA = -1.0
_RARITY_NODATA = -64329.0
_SCALING_PARAM = 2.5


def execute(args):
    """Habitat Quality.

    Open files necessary for the portion of the habitat_quality
    model.

    Args:
        workspace_dir (string): a path to the directory that will write output
            and other temporary files (required)
        lulc_cur_path (string): a path to an input land use/land cover raster
            (required)
        lulc_fut_path (string): a path to an input land use/land cover raster
            (optional)
        lulc_bas_path (string): a path to an input land use/land cover raster
            (optional, but required for rarity calculations)
        threat_folder (string): a path to the directory that will contain all
            threat rasters (required)
        threats_table_path (string): a path to an input CSV containing data
            of all the considered threats. Each row is a degradation source
            and each column a different attribute of the source with the
            following names: 'THREAT','MAX_DIST','WEIGHT' (required).
        access_vector_path (string): a path to an input polygon shapefile
            containing data on the relative protection against threats (optional)
        sensitivity_table_path (string): a path to an input CSV file of LULC
            types, whether they are considered habitat, and their sensitivity
            to each threat (required)
        half_saturation_constant (float): a python float that determines
            the spread and central tendency of habitat quality scores
            (required)
        suffix (string): a python string that will be inserted into all
            raster path paths just before the file extension.

    Example Args Dictionary::

        {
            'workspace_dir': 'path/to/workspace_dir',
            'lulc_cur_path': 'path/to/lulc_cur_raster',
            'lulc_fut_path': 'path/to/lulc_fut_raster',
            'lulc_bas_path': 'path/to/lulc_bas_raster',
            'threat_raster_folder': 'path/to/threat_rasters/',
            'threats_table_path': 'path/to/threats_csv',
            'access_vector_path': 'path/to/access_shapefile',
            'sensitivity_table_path': 'path/to/sensitivity_csv',
            'half_saturation_constant': 0.5,
            'suffix': '_results',
        }

    Returns:
        None
    """
    workspace = args['workspace_dir']

    # Append a _ to the suffix if it's not empty and doesn't already have one
    suffix = utils.make_suffix_string(args, 'suffix')

    # Check to see if each of the workspace folders exists.  If not, create the
    # folder in the filesystem.
    inter_dir = os.path.join(workspace, 'intermediate')
    out_dir = os.path.join(workspace, 'output')
    kernel_dir = os.path.join(inter_dir, 'kernels')
    utils.make_directories([inter_dir, out_dir, kernel_dir])

    # get a handle on the folder with the threat rasters
    threat_raster_dir = args['threat_raster_folder']

    threat_dict = utils.build_lookup_from_csv(
        args['threats_table_path'], 'THREAT', to_lower=False)
    sensitivity_dict = utils.build_lookup_from_csv(
        args['sensitivity_table_path'], 'LULC', to_lower=False)

    # check that the required headers exist in the sensitivity table.
    # Raise exception if they don't.
    sens_header_list = sensitivity_dict.items()[0][1].keys()
    required_sens_header_list = ['LULC', 'NAME', 'HABITAT']
    missing_sens_header_list = [
        h for h in required_sens_header_list if h not in sens_header_list]
    if missing_sens_header_list:
        raise ValueError(
            'Column(s) %s are missing in the sensitivity table' %
            (', '.join(missing_sens_header_list)))

    # check that the threat names in the threats table match with the threats
    # columns in the sensitivity table. Raise exception if they don't.
    for threat in threat_dict:
        if 'L_' + threat not in sens_header_list:
            missing_threat_header_list = (
                set(sens_header_list) - set(required_sens_header_list))
            raise ValueError(
                'Threat "%s" does not match any column in the sensitivity '
                'table. Possible columns: %s' %
                (threat, missing_threat_header_list))

    # get the half saturation constant
    try:
        half_saturation = float(args['half_saturation_constant'])
    except ValueError:
        raise ValueError('Half-saturation constant is not a numeric number.'
                         'It is: %s' % args['half_saturation_constant'])

    # declare dictionaries to store the land cover and the threat rasters
    # pertaining to the different threats
    lulc_path_dict = {}
    threat_path_dict = {}
    # also store land cover and threat rasters in a list
    lulc_and_threat_raster_list = []
    aligned_raster_list = []
    # declare a set to store unique codes from lulc rasters
    raster_unique_lucodes = set()

    # compile all the threat rasters associated with the land cover
    for lulc_key, lulc_args in (('_c', 'lulc_cur_path'),
                                ('_f', 'lulc_fut_path'),
                                ('_b', 'lulc_bas_path')):
        if lulc_args in args:
            lulc_path = args[lulc_args]
            lulc_path_dict[lulc_key] = lulc_path
            # save land cover paths in a list for alignment and resize
            lulc_and_threat_raster_list.append(lulc_path)
            aligned_raster_list.append(
                os.path.join(
                    inter_dir, os.path.basename(lulc_path).replace(
                        '.tif', '_aligned.tif')))

            # save unique codes to check if it's missing in sensitivity table
            for _, lulc_block in pygeoprocessing.iterblocks((lulc_path, 1)):
                raster_unique_lucodes.update(numpy.unique(lulc_block))

            # Remove the nodata value from the set of landuser codes.
            nodata = pygeoprocessing.get_raster_info(lulc_path)['nodata'][0]
            try:
                raster_unique_lucodes.remove(nodata)
            except KeyError:
                # KeyError when the nodata value was not encountered in the
                # raster's pixel values.  Same result when nodata value is
                # None.
                pass

            # add a key to the threat dictionary that associates all threat
            # rasters with this land cover
            threat_path_dict['threat' + lulc_key] = {}

            # for each threat given in the CSV file try opening the associated
            # raster which should be found in threat_raster_folder
            for threat in threat_dict:
                # it's okay to have no threat raster for baseline scenario
                threat_path_dict['threat' + lulc_key][threat] = (
                    resolve_ambiguous_raster_path(
                        os.path.join(threat_raster_dir, threat + lulc_key),
                        raise_error=(lulc_key != '_b')))

                # save threat paths in a list for alignment and resize
                threat_path = threat_path_dict['threat' + lulc_key][threat]
                if threat_path:
                    lulc_and_threat_raster_list.append(threat_path)
                    aligned_raster_list.append(
                        os.path.join(
                            inter_dir, os.path.basename(lulc_path).replace(
                                '.tif', '_aligned.tif')))
    # check if there's any lucode from the LULC rasters missing in the
    # sensitivity table
    table_unique_lucodes = set(sensitivity_dict.keys())
    missing_lucodes = raster_unique_lucodes.difference(table_unique_lucodes)
    if missing_lucodes:
        raise ValueError(
            'The following land cover codes were found in your landcover rasters '
            'but not in your sensitivity table. Check your sensitivity table '
            'to see if they are missing: %s. \n\n' %
            ', '.join([str(x) for x in sorted(missing_lucodes)]))

    # Align and resize all the land cover and threat rasters,
    # and tore them in the intermediate folder
    LOGGER.info('Starting aligning and resizing land cover and threat rasters')

    lulc_pixel_size = (
        pygeoprocessing.get_raster_info(args['lulc_cur_path']))['pixel_size']

    aligned_raster_list = [
        os.path.join(inter_dir, os.path.basename(path).replace(
            '.tif', '_aligned.tif')) for path in lulc_and_threat_raster_list]

    pygeoprocessing.align_and_resize_raster_stack(
        lulc_and_threat_raster_list, aligned_raster_list,
        ['near']*len(lulc_and_threat_raster_list), lulc_pixel_size,
        'intersection')

    LOGGER.info('Finished aligning and resizing land cover and threat rasters')

    # Modify paths in lulc_path_dict and threat_path_dict to be aligned rasters
    for lulc_key, lulc_path in lulc_path_dict.iteritems():
        lulc_path_dict[lulc_key] = os.path.join(
            inter_dir, os.path.basename(lulc_path).replace(
                '.tif', '_aligned.tif'))
        for threat in threat_dict:
            threat_path = threat_path_dict['threat' + lulc_key][threat]
            if threat_path in lulc_and_threat_raster_list:
                threat_path_dict['threat' + lulc_key][threat] = os.path.join(
                    inter_dir, os.path.basename(threat_path).replace(
                        '.tif', '_aligned.tif'))

    LOGGER.info('Starting habitat_quality biophysical calculations')

    # Rasterize access vector, if value is null set to 1 (fully accessible),
    # else set to the value according to the ACCESS attribute
    cur_lulc_path = lulc_path_dict['_c']
    fill_value = 1.0
    try:
        LOGGER.info('Handling Access Shape')
        access_raster_path = os.path.join(
            inter_dir, 'access_layer%s.tif' % suffix)
        # create a new raster based on the raster info of current land cover
        pygeoprocessing.new_raster_from_base(
            cur_lulc_path, access_raster_path, gdal.GDT_Float32,
            [_OUT_NODATA], fill_value_list=[fill_value])
        pygeoprocessing.rasterize(
            args['access_vector_path'], access_raster_path, burn_values=None,
            option_list=['ATTRIBUTE=ACCESS'])

    except KeyError:
        LOGGER.info(
            'No Access Shape Provided, access raster filled with 1s.')

    # calculate the weight sum which is the sum of all the threats' weights
    weight_sum = 0.0
    for threat_data in threat_dict.itervalues():
        # Sum weight of threats
        weight_sum = weight_sum + threat_data['WEIGHT']

    LOGGER.debug('lulc_path_dict : %s', lulc_path_dict)

    # for each land cover raster provided compute habitat quality
    for lulc_key, lulc_path in lulc_path_dict.iteritems():
        LOGGER.info('Calculating habitat quality for landuse: %s', lulc_path)

        # Create raster of habitat based on habitat field
        habitat_raster_path = os.path.join(
            inter_dir, 'habitat%s%s.tif' % (lulc_key, suffix))
        map_raster_to_dict_values(
            lulc_path, habitat_raster_path, sensitivity_dict, 'HABITAT',
            _OUT_NODATA, values_required=False)

        # initialize a list that will store all the threat/threat rasters
        # after they have been adjusted for distance, weight, and access
        deg_raster_list = []

        # a list to keep track of the normalized weight for each threat
        weight_list = numpy.array([])

        # variable to indicate whether we should break out of calculations
        # for a land cover because a threat raster was not found
        exit_landcover = False

        # adjust each threat/threat raster for distance, weight, and access
        for threat, threat_data in threat_dict.iteritems():
            LOGGER.info('Calculating threat: %s.\nThreat data: %s' %
                        (threat, threat_data))

            # get the threat raster for the specific threat
            threat_raster_path = threat_path_dict['threat' + lulc_key][threat]
            LOGGER.info('threat_raster_path %s', threat_raster_path)
            if threat_raster_path is None:
                LOGGER.info(
                    'The threat raster for %s could not be found for the land '
                    'cover %s. Skipping Habitat Quality calculation for this '
                    'land cover.' % (threat, lulc_key))
                exit_landcover = True
                break

            # need the pixel size for the threat raster so we can create
            # an appropriate kernel for convolution
            threat_pixel_size = pygeoprocessing.get_raster_info(
                threat_raster_path)['pixel_size']
            # pixel size tuple could have negative value
            mean_threat_pixel_size = (
                abs(threat_pixel_size[0]) + abs(threat_pixel_size[1]))/2.0

            # convert max distance (given in KM) to meters
            max_dist_m = threat_data['MAX_DIST'] * 1000.0

            # convert max distance from meters to the number of pixels that
            # represents on the raster
            max_dist_pixel = max_dist_m / mean_threat_pixel_size
            LOGGER.debug('Max distance in pixels: %f', max_dist_pixel)

            # blur the threat raster based on the effect of the threat over
            # distance
            decay_type = threat_data['DECAY']
            kernel_path = os.path.join(
                kernel_dir, 'kernel_%s%s%s.tif' % (threat, lulc_key, suffix))
            if decay_type == 'linear':
                make_linear_decay_kernel_path(max_dist_pixel, kernel_path)
            elif decay_type == 'exponential':
                utils.exponential_decay_kernel_raster(
                    max_dist_pixel, kernel_path)
            else:
                raise ValueError(
                    "Unknown type of decay in biophysical table, should be "
                    "either 'linear' or 'exponential'. Input was %s for threat"
                    " %s." % (decay_type, threat))

            filtered_threat_raster_path = os.path.join(
                inter_dir, 'filtered_%s%s%s.tif' % (threat, lulc_key, suffix))
            pygeoprocessing.convolve_2d(
                (threat_raster_path, 1), (kernel_path, 1),
                filtered_threat_raster_path)

            # create sensitivity raster based on threat
            sens_raster_path = os.path.join(
                inter_dir, 'sens_%s%s%s.tif' % (threat, lulc_key, suffix))
            map_raster_to_dict_values(
                lulc_path, sens_raster_path, sensitivity_dict, 'L_' + threat,
                _OUT_NODATA, values_required=True)

            # get the normalized weight for each threat
            weight_avg = threat_data['WEIGHT'] / weight_sum

            # add the threat raster adjusted by distance and the raster
            # representing sensitivity to the list to be past to
            # vectorized_rasters below
            deg_raster_list.append(filtered_threat_raster_path)
            deg_raster_list.append(sens_raster_path)

            # store the normalized weight for each threat in a list that
            # will be used below in total_degradation
            weight_list = numpy.append(weight_list, weight_avg)

        # check to see if we got here because a threat raster was missing
        # and if so then we want to skip to the next landcover
        if exit_landcover:
            continue

        def total_degradation(*raster):
            """A vectorized function that computes the degradation value for
                each pixel based on each threat and then sums them together

                *rasters - a list of floats depicting the adjusted threat
                    value per pixel based on distance and sensitivity.
                    The values are in pairs so that the values for each threat
                    can be tracked:
                    [filtered_val_threat1, sens_val_threat1,
                     filtered_val_threat2, sens_val_threat2, ...]
                    There is an optional last value in the list which is the
                    access_raster value, but it is only present if
                    access_raster is not None.

                returns - the total degradation score for the pixel"""

            # we can not be certain how many threats the user will enter,
            # so we handle each filtered threat and sensitivity raster
            # in pairs
            sum_degradation = numpy.zeros(raster[0].shape)
            for index in range(len(raster) / 2):
                step = index * 2
                sum_degradation += (
                    raster[step] * raster[step + 1] * weight_list[index])

            nodata_mask = numpy.empty(raster[0].shape, dtype=numpy.int8)
            nodata_mask[:] = 0
            for array in raster:
                nodata_mask = nodata_mask | (array == _OUT_NODATA)

            # the last element in raster is access
            return numpy.where(
                    nodata_mask, _OUT_NODATA, sum_degradation * raster[-1])

        # add the access_raster onto the end of the collected raster list. The
        # access_raster will be values from the shapefile if provided or a
        # raster filled with all 1's if not
        deg_raster_list.append(access_raster_path)

        deg_sum_raster_path = os.path.join(
            out_dir, 'deg_sum' + lulc_key + suffix + '.tif')

        LOGGER.info('Starting raster calculation on total_degradation')

        deg_raster_band_list = [(path, 1) for path in deg_raster_list]
        pygeoprocessing.raster_calculator(
            deg_raster_band_list, total_degradation,
            deg_sum_raster_path, gdal.GDT_Float32, _OUT_NODATA)

        LOGGER.info('Finished raster calculation on total_degradation')

        # Compute habitat quality
        # ksq: a term used below to compute habitat quality
        ksq = half_saturation**_SCALING_PARAM

        def quality_op(degradation, habitat):
            """Vectorized function that computes habitat quality given
                a degradation and habitat value.

                degradation - a float from the created degradation
                    raster above.
                habitat - a float indicating habitat suitability from
                    from the habitat raster created above.

                returns - a float representing the habitat quality
                    score for a pixel
            """
            degredataion_clamped = numpy.where(degradation < 0, 0, degradation)

            return numpy.where(
                    (degradation == _OUT_NODATA) | (habitat == _OUT_NODATA),
                    _OUT_NODATA,
                    (habitat * (1.0 - ((degredataion_clamped**_SCALING_PARAM) /
                     (degredataion_clamped**_SCALING_PARAM + ksq)))))

        quality_path = os.path.join(
            out_dir, 'quality' + lulc_key + suffix + '.tif')

        LOGGER.info('Starting raster calculation on quality_op')

        deg_hab_raster_list = [deg_sum_raster_path, habitat_raster_path]

        deg_hab_raster_band_list = [
            (path, 1) for path in deg_hab_raster_list]
        pygeoprocessing.raster_calculator(
            deg_hab_raster_band_list, quality_op, quality_path,
            gdal.GDT_Float32, _OUT_NODATA)

        LOGGER.info('Finished raster calculation on quality_op')

    # Compute Rarity if user supplied baseline raster
    if '_b' not in lulc_path_dict:
        LOGGER.info('Baseline not provided to compute Rarity')
    else:
        lulc_base_path = lulc_path_dict['_b']

        # get the area of a base pixel to use for computing rarity where the
        # pixel sizes are different between base and cur/fut rasters
        base_pixel_size = pygeoprocessing.get_raster_info(
            lulc_base_path)['pixel_size']
        base_area = float(abs(base_pixel_size[0]) * abs(base_pixel_size[1]))
        base_nodata = pygeoprocessing.get_raster_info(
            lulc_base_path)['nodata'][0]

        lulc_code_count_b = raster_pixel_count(lulc_base_path)

        # compute rarity for current landscape and future (if provided)
        for lulc_key in ['_c', '_f']:
            if lulc_key not in lulc_path_dict:
                continue
            lulc_path = lulc_path_dict[lulc_key]
            lulc_time = 'current' if lulc_key == '_c' else 'future'

            # get the area of a cur/fut pixel
            lulc_pixel_size = pygeoprocessing.get_raster_info(
                lulc_path)['pixel_size']
            lulc_area = float(abs(lulc_pixel_size[0]) * abs(lulc_pixel_size[1]))
            lulc_nodata = pygeoprocessing.get_raster_info(
                lulc_path)['nodata'][0]

            def trim_op(base, cover_x):
                """Trim cover_x to the mask of base.

                Parameters:
                    base (numpy.ndarray): base raster from 'lulc_base'
                    cover_x (numpy.ndarray): either future or current land
                        cover raster from 'lulc_path' above

                Returns:
                    _OUT_NODATA where either array has nodata, otherwise
                    cover_x.
                """
                return numpy.where(
                    (base == base_nodata) | (cover_x == lulc_nodata),
                    base_nodata, cover_x)

            LOGGER.info('Create new cover for %s', lulc_path)

            new_cover_path = os.path.join(
                inter_dir, 'new_cover' + lulc_key + suffix + '.tif')

            LOGGER.info('Starting masking %s land cover to base land cover.'
                        % lulc_time)

            pygeoprocessing.raster_calculator(
                [(lulc_base_path, 1), (lulc_path, 1)], trim_op, new_cover_path,
                gdal.GDT_Float32, _OUT_NODATA)

            LOGGER.info('Finished masking %s land cover to base land cover.'
                        % lulc_time)

            LOGGER.info('Starting rarity computation on %s land cover.'
                        % lulc_time)

            lulc_code_count_x = raster_pixel_count(new_cover_path)

            # a dictionary to map LULC types to a number that depicts how
            # rare they are considered
            code_index = {}

            # compute rarity index for each lulc code
            # define 0.0 if an lulc code is found in the cur/fut landcover
            # but not the baseline
            for code in lulc_code_count_x.iterkeys():
                if code in lulc_code_count_b:
                    numerator = lulc_code_count_x[code] * lulc_area
                    denominator = lulc_code_count_b[code] * base_area
                    ratio = 1.0 - (numerator / denominator)
                    code_index[code] = ratio
                else:
                    code_index[code] = 0.0

            rarity_path = os.path.join(
                out_dir, 'rarity' + lulc_key + suffix + '.tif')

            pygeoprocessing.reclassify_raster(
                (new_cover_path, 1), code_index, rarity_path, gdal.GDT_Float32,
                _RARITY_NODATA)

            LOGGER.info('Finished rarity computation on %s land cover.'
                        % lulc_time)
    LOGGER.info('Finished habitat_quality biophysical calculations')


def resolve_ambiguous_raster_path(path, raise_error=True):
    """Determine real path when we don't know true path extension.

    Parameters:
        path (string): file path that includes the name of the file but not
            its extension

        raise_error (boolean): if True then function will raise an
            ValueError if a valid raster file could not be found.

    Return:
        the full path, plus extension, to the valid raster.
    """
    # Turning on exceptions so that if an error occurs when trying to open a
    # file path we can catch it and handle it properly

    def _error_handler(*_):
        """A dummy error handler that raises a ValueError."""
        raise ValueError()
    gdal.PushErrorHandler(_error_handler)

    # a list of possible extensions for raster datasets. We currently can
    # handle .tif and directory paths
    possible_ext = ['', '.tif', '.img']

    # initialize dataset to None in the case that all paths do not exist
    dataset = None
    for ext in possible_ext:
        full_path = path + ext
        if not os.path.exists(full_path):
            continue
        try:
            dataset = gdal.OpenEx(full_path, gdal.GA_ReadOnly)
            break
        except ValueError:
            # If GDAL can't open the raster, our GDAL error handler will be
            # executed and ValueError raised.
            continue

    gdal.PopErrorHandler()

    # If a dataset comes back None, then it could not be found / opened and we
    # should fail gracefully
    if dataset is None:
        if raise_error:
            raise ValueError(
                'There was an Error locating a threat raster in the input '
                'folder. One of the threat names in the CSV table does not '
                'match to a threat raster in the input folder. Please check '
                'that the names correspond. The threat raster that could not '
                'be found is: %s' % path)
        else:
            full_path = None

    dataset = None
    return full_path


def raster_pixel_count(raster_path):
    """Count unique pixel values in raster.

    Parameters:
        raster_path (string): path to a raster

    Returns:
        dict of pixel values to frequency.
    """
    nodata = pygeoprocessing.get_raster_info(raster_path)['nodata'][0]
    counts = collections.defaultdict(int)
    for _, raster_block in pygeoprocessing.iterblocks((raster_path, 1)):
        for value, count in zip(
                *numpy.unique(raster_block, return_counts=True)):
            if value == nodata:
                continue
            counts[value] += count
    return counts


def map_raster_to_dict_values(
        key_raster_path, out_path, attr_dict, field, out_nodata, values_required):
    """Creates a new raster from 'key_raster' where the pixel values from
       'key_raster' are the keys to a dictionary 'attr_dict'. The values
       corresponding to those keys is what is written to the new raster. If a
       value from 'key_raster' does not appear as a key in 'attr_dict' then
       raise an Exception if 'raise_error' is True, otherwise return a
       'out_nodata'

       key_raster_path - a GDAL raster path dataset whose pixel values relate to
                     the keys in 'attr_dict'
       out_path - a string for the output path of the created raster
       attr_dict - a dictionary representing a table of values we are interested
                   in making into a raster
       field - a string of which field in the table or key in the dictionary
               to use as the new raster pixel values
       out_nodata - a floating point value that is the nodata value.
       raise_error - a string that decides how to handle the case where the
           value from 'key_raster' is not found in 'attr_dict'. If 'raise_error'
           is 'values_required', raise Exception, if 'none', return 'out_nodata'

       returns - a GDAL raster, or raises an Exception and fail if:
           1) raise_error is True and
           2) the value from 'key_raster' is not a key in 'attr_dict'
    """

    LOGGER.info('Starting map_raster_to_dict_values')
    int_attr_dict = {}
    for key in attr_dict:
        int_attr_dict[int(key)] = float(attr_dict[key][field])

    pygeoprocessing.reclassify_raster(
        (key_raster_path, 1), int_attr_dict, out_path, gdal.GDT_Float32,
        out_nodata, values_required)


def make_linear_decay_kernel_path(max_distance, kernel_path):
    """Create a linear decay kernel as a raster.

    Pixels in raster are equal to d / max_distance where d is the distance to
    the center of the raster in number of pixels.

    Parameters:
        max_distance (int): number of pixels out until the decay is 0.
        kernel_path (string): path to output raster whose values are in (0,1)
            representing distance to edge.  Size is (max_distance * 2 + 1)^2

    Returns:
        None
    """
    kernel_size = int(numpy.round(max_distance * 2 + 1))

    driver = gdal.GetDriverByName('GTiff')
    kernel_dataset = driver.Create(
        kernel_path.encode('utf-8'), kernel_size, kernel_size, 1,
        gdal.GDT_Float32, options=['BIGTIFF=IF_SAFER'])

    # Make some kind of geotransform, it doesn't matter what but
    # will make GIS libraries behave better if it's all defined
    kernel_dataset.SetGeoTransform([444720, 30, 0, 3751320, 0, -30])
    srs = osr.SpatialReference()
    srs.SetUTM(11, 1)
    srs.SetWellKnownGeogCS('NAD27')
    kernel_dataset.SetProjection(srs.ExportToWkt())

    kernel_band = kernel_dataset.GetRasterBand(1)
    kernel_band.SetNoDataValue(-9999)

    col_index = numpy.array(xrange(kernel_size))
    integration = 0.0
    for row_index in xrange(kernel_size):
        distance_kernel_row = numpy.sqrt(
            (row_index - max_distance) ** 2 +
            (col_index - max_distance) ** 2).reshape(1, kernel_size)
        kernel = numpy.where(
            distance_kernel_row > max_distance, 0.0,
            (max_distance - distance_kernel_row) / max_distance)
        integration += numpy.sum(kernel)
        kernel_band.WriteArray(kernel, xoff=0, yoff=row_index)

    for row_index in xrange(kernel_size):
        kernel_row = kernel_band.ReadAsArray(
            xoff=0, yoff=row_index, win_xsize=kernel_size, win_ysize=1)
        kernel_row /= integration
        kernel_band.WriteArray(kernel_row, 0, row_index)


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate args to ensure they conform to `execute`'s contract.

    Parameters:
        args (dict): dictionary of key(str)/value pairs where keys and
            values are specified in `execute` docstring.
        limit_to (str): (optional) if not None indicates that validation
            should only occur on the args[limit_to] value. The intent that
            individual key validation could be significantly less expensive
            than validating the entire `args` dictionary.

    Returns:
        list of ([invalid key_a, invalid_keyb, ...], 'warning/error message')
            tuples. Where an entry indicates that the invalid keys caused
            the error message in the second part of the tuple. This should
            be an empty list if validation succeeds.
    """
    missing_key_list = []
    no_value_list = []
    validation_error_list = []

    # required args
    for key in [
            'workspace_dir',
            'lulc_cur_path',
            'threat_raster_folder',
            'threats_table_path',
            'sensitivity_table_path',
            'half_saturation_constant']:
        if limit_to is None or limit_to == key:
            if key not in args:
                missing_key_list.append(key)
            elif args[key] in ['', None]:
                no_value_list.append(key)

    if len(missing_key_list) > 0:
        # if there are missing keys, we have raise KeyError to stop hard
        raise KeyError(
            "The following keys were expected in `args` but were missing" +
            ', '.join(missing_key_list))

    # check for required file existence:
    for key in [
            'lulc_cur_path',
            'threat_raster_folder',
            'threats_table_path',
            'sensitivity_table_path']:
        if (limit_to is None or limit_to == key) and (
                not os.path.exists(args[key])):
            validation_error_list.append(
                ([key], 'not found on disk'))

    # check that existing/optional files are the correct types
    with utils.capture_gdal_logging():
        for key, key_type in [
                ('lulc_cur_path', 'raster'),
                ('lulc_fut_path', 'raster'),
                ('lulc_bas_path', 'raster'),
                ('access_vector_path', 'vector')]:
            if (limit_to is None or limit_to == key) and key in args:
                if not os.path.exists(args[key]):
                    validation_error_list.append(
                        ([key], 'not found on disk'))
                    continue
                if key_type == 'raster':
                    raster = gdal.OpenEx(args[key])
                    if raster is None:
                        validation_error_list.append(
                            ([key], 'not a raster'))
                    del raster
                elif key_type == 'vector':
                    vector = gdal.OpenEx(args[key])
                    if vector is None:
                        validation_error_list.append(
                            ([key], 'not a vector'))
                    del vector

    return validation_error_list
