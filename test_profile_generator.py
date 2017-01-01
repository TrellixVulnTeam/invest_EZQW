"""Profile generator test."""
import logging

import natcap.invest.profile_generator

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')

LOGGER = logging.getLogger('test_profile_generator')


def main():
    """Entry point."""
    args = {
        'workspace_dir': 'profile_generator_workspace',
        'results_suffix': 'adaptive_steps',
        #'bathymetry_path': r"C:\Users\rpsharp\Documents\clipped_claybark.tif",
        'bathymetry_path': r"clipped_claybark_dem.tif",
        #'bathymetry_path': r"E:\repositories\bitbucket_repos\invest\data\invest-data\Base_Data\Marine\DEMs\claybark_dem",
        'shore_height': 0.0,  # shore elevation on bathy layer
        'representative_point_vector_path': r"D:\Dropbox\shared_with_users\profile_data_for_jess\representative_profile_points.shp",
        # stepsize is (close distance step, max close distance)
        # stepsize is (far distance step, far distance definition)
        'step_size': ((10, 500), (100, 2000)),
        'smoothing_sigma': 0.0,  # sigma of gaussian filter of bathy layer
        'offshore_profile_length': 2000,
        'onshore_profile_length': 500,
        'habitat_vector_path_list': [
            (r"D:\Dropbox\shared_with_users\profile_data_for_jess\sample_claybark_hab_a.shp", 'name')],
    }
    natcap.invest.profile_generator.execute(args)

if __name__ == '__main__':
    main()
