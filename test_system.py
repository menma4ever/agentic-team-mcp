"""Compatibility entry point for the behavioral regression suite."""
import unittest
from tests import test_engine
if __name__ == '__main__':
    unittest.main(module=test_engine, verbosity=2)

