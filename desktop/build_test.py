import unittest

from build import validate_build_python


class ValidateBuildPythonTests(unittest.TestCase):
    def test_rejects_python_39(self):
        with self.assertRaisesRegex(RuntimeError, "Python 3.10\\+"):
            validate_build_python((3, 9), "3.9.0", "C:/Python39/python.exe")

    def test_accepts_python_312(self):
        validate_build_python((3, 12), "3.12.0", "C:/Python312/python.exe")


if __name__ == "__main__":
    unittest.main()
