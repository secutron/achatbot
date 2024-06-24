import os
import asyncio
import logging

import unittest

from src.cmd.init import Env
from src.common.logger import Logger

r"""
python -m unittest test.cmd.test_init.TestEnv.test_save_to_yaml
"""


class TestEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Logger.init(logging.DEBUG, is_file=False)
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_save_to_yaml(self):
        os.environ['RECORDER_TAG'] = 'wakeword_rms_recorder'
        os.environ['CONF_ENV'] = 'local'
        res = asyncio.run(Env.save_to_yaml())
        self.assertIsInstance(res, list)
        for file_path in res:
            print(file_path)
            self.assertIsNotNone(file_path)
