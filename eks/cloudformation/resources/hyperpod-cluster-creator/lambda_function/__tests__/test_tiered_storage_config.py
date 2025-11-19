"""
Unit tests for TieredStorageConfig functionality in the HyperPod cluster Lambda function
"""
import unittest
import json
import os
from unittest.mock import patch, MagicMock, Mock
import sys

# Mock cfnresponse before importing lambda_function
sys.modules['cfnresponse'] = Mock()

# Add the lambda_function directory to the path
sys.path.insert(0, os.path.dirname(__file__))

from lambda_function import get_tiered_storage_config_from_env


class TestTieredStorageConfig(unittest.TestCase):
    """Test cases for TieredStorageConfig validation and parsing"""

    def setUp(self):
        """Set up test fixtures"""
        # Clear any existing TIERED_STORAGE_CONFIG environment variable
        if 'TIERED_STORAGE_CONFIG' in os.environ:
            del os.environ['TIERED_STORAGE_CONFIG']

    def tearDown(self):
        """Clean up after tests"""
        if 'TIERED_STORAGE_CONFIG' in os.environ:
            del os.environ['TIERED_STORAGE_CONFIG']

    def test_valid_config_with_both_fields(self):
        """Test valid TieredStorageConfig with both InstanceMemoryAllocationPercentage and Mode"""
        config = {
            "InstanceMemoryAllocationPercentage": 50,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNotNone(result)
        self.assertEqual(result['InstanceMemoryAllocationPercentage'], 50)
        self.assertEqual(result['Mode'], 'Enable')

    def test_valid_config_with_only_percentage(self):
        """Test valid TieredStorageConfig with only InstanceMemoryAllocationPercentage"""
        config = {
            "InstanceMemoryAllocationPercentage": 75
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNotNone(result)
        self.assertEqual(result['InstanceMemoryAllocationPercentage'], 75)
        self.assertNotIn('Mode', result)

    def test_valid_config_with_only_mode(self):
        """Test valid TieredStorageConfig with only Mode"""
        config = {
            "Mode": "Disable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNotNone(result)
        self.assertEqual(result['Mode'], 'Disable')
        self.assertNotIn('InstanceMemoryAllocationPercentage', result)

    def test_empty_config(self):
        """Test empty TieredStorageConfig"""
        os.environ['TIERED_STORAGE_CONFIG'] = ''
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_no_config_env_var(self):
        """Test when TIERED_STORAGE_CONFIG environment variable is not set"""
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_invalid_percentage_negative(self):
        """Test invalid InstanceMemoryAllocationPercentage (negative value)"""
        config = {
            "InstanceMemoryAllocationPercentage": -10,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_invalid_percentage_over_100(self):
        """Test invalid InstanceMemoryAllocationPercentage (over 100)"""
        config = {
            "InstanceMemoryAllocationPercentage": 150,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_invalid_percentage_float(self):
        """Test invalid InstanceMemoryAllocationPercentage (float instead of int)"""
        config = {
            "InstanceMemoryAllocationPercentage": 50.5,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_invalid_mode_value(self):
        """Test invalid Mode value (not 'Enable' or 'Disable')"""
        config = {
            "InstanceMemoryAllocationPercentage": 50,
            "Mode": "Active"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_invalid_json_format(self):
        """Test invalid JSON format"""
        os.environ['TIERED_STORAGE_CONFIG'] = '{invalid json}'
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_config_not_dict(self):
        """Test when config is not a dictionary"""
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(["not", "a", "dict"])
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNone(result)

    def test_boundary_percentage_zero(self):
        """Test boundary value: InstanceMemoryAllocationPercentage = 0"""
        config = {
            "InstanceMemoryAllocationPercentage": 0,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNotNone(result)
        self.assertEqual(result['InstanceMemoryAllocationPercentage'], 0)

    def test_boundary_percentage_100(self):
        """Test boundary value: InstanceMemoryAllocationPercentage = 100"""
        config = {
            "InstanceMemoryAllocationPercentage": 100,
            "Mode": "Enable"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        self.assertIsNotNone(result)
        self.assertEqual(result['InstanceMemoryAllocationPercentage'], 100)

    def test_unexpected_fields_warning(self):
        """Test that unexpected fields generate a warning but don't fail validation"""
        config = {
            "InstanceMemoryAllocationPercentage": 50,
            "Mode": "Enable",
            "UnexpectedField": "value"
        }
        os.environ['TIERED_STORAGE_CONFIG'] = json.dumps(config)
        
        result = get_tiered_storage_config_from_env()
        
        # Should still return the config despite unexpected fields
        self.assertIsNotNone(result)
        self.assertEqual(result['InstanceMemoryAllocationPercentage'], 50)
        self.assertEqual(result['Mode'], 'Enable')
        self.assertIn('UnexpectedField', result)


if __name__ == '__main__':
    unittest.main()
