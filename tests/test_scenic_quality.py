"""Module for Regression Testing the InVEST Scenic Quality module."""
import unittest
import tempfile
import shutil
import os
import csv

from osgeo import ogr
import pygeoprocessing.testing
from pygeoprocessing.testing import scm
from pygeoprocessing.testing import sampledata
from shapely.geometry import Polygon

from nose.tools import nottest

SAMPLE_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-data')
REGRESSION_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-test-data', 'scenic_quality')


class ScenicQualityUnitTests(unittest.TestCase):
    """Unit tests for Scenic Quality Model."""

    def setUp(self):
        """Overriding setUp function to create temporary workspace directory."""
        # this lets us delete the workspace after its done no matter the
        # the rest result
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Overriding tearDown function to remove temporary directory."""
        shutil.rmtree(self.workspace_dir)


    def test_add_percent_overlap(self):
        """SQ: test 'add_percent_overlap' function."""
        from natcap.invest.scenic_quality import scenic_quality

        temp_dir = self.workspace_dir
        srs = sampledata.SRS_WILLAMETTE

        pos_x = srs.origin[0]
        pos_y = srs.origin[1]
        fields = {'myid': 'int'}
        attrs = [{'myid': 1}, {'myid': 2}]

        # Create geometry for the polygons
        geom = [
            Polygon(
                [(pos_x, pos_y), (pos_x + 60, pos_y), (pos_x + 60, pos_y - 60),
                 (pos_x, pos_y - 60), (pos_x, pos_y)]),
            Polygon(
                [(pos_x + 80, pos_y - 80), (pos_x + 140, pos_y - 80),
                 (pos_x + 140, pos_y - 140), (pos_x + 80, pos_y - 140),
                 (pos_x + 80, pos_y - 80)])]

        shape_path = os.path.join(temp_dir, 'shape.shp')
        # Create the point shapefile
        shape_path = pygeoprocessing.testing.create_vector_on_disk(
            geom, srs.projection, fields, attrs,
            vector_format='ESRI Shapefile', filename=shape_path)

        pixel_counts = {1: 2, 2: 1}
        pixel_size = 30

        scenic_quality.add_percent_overlap(
            shape_path, 'myid', '%_overlap', pixel_counts, pixel_size)

        exp_result = {1: 50.0, 2: 25.0}

        shape = ogr.Open(shape_path)
        layer = shape.GetLayer()
        for feat in layer:
            myid = feat.GetFieldAsInteger('myid')
            perc_overlap = feat.GetFieldAsDouble('%_overlap')
            self.assertEqual(exp_result[myid], perc_overlap)


class ScenicQualityRegressionTests(unittest.TestCase):
    """Regression Tests for Scenic Quality Model."""

    def setUp(self):
        """Overriding setUp function to create temporary workspace directory."""
        # this lets us delete the workspace after its done no matter the
        # the rest result
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Overriding tearDown function to remove temporary directory."""
        shutil.rmtree(self.workspace_dir)

    @staticmethod
    def generate_base_args(workspace_dir):
        """Generate an args list that is consistent across regression tests."""
        args = {
            'workspace_dir': workspace_dir,
            'aoi_path': os.path.join(
                SAMPLE_DATA, 'ScenicQuality', 'Input', 'AOI_WCVI.shp'),
            'structure_path': os.path.join(
                SAMPLE_DATA, 'ScenicQuality', 'Input', 'AquaWEM_points.shp'),
            'keep_feat_viewsheds': 'No',
            'keep_val_viewsheds': 'No',
            'dem_path': os.path.join(
                SAMPLE_DATA, 'Base_Data', 'Marine', 'DEMs', 'claybark_dem'),
            'refraction': 0.13,
            'max_valuation_radius': 8000
        }

        return args

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    @nottest
    def test_scenic_quality_no_refraction(self):
        """SQ: testing stuff."""
        from natcap.invest.scenic_quality import scenic_quality

        args = ScenicQualityTests.generate_base_args(self.workspace_dir)

        scenic_quality.execute(args)

    def test_scenic_quality(self):
        """SQ: testing stuff."""
        from natcap.invest.scenic_quality import scenic_quality

        args = ScenicQualityTests.generate_base_args(self.workspace_dir)

        scenic_quality.execute(args)
