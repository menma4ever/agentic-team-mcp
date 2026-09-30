"""Compatibility entry point for real HTTP/MCP integration."""
import unittest
from tests import test_service
if __name__ == '__main__':
    unittest.main(module=test_service, verbosity=2)

