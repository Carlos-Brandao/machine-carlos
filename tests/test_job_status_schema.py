import unittest

from machine_admin.models import Job


class JobStatusSchemaTests(unittest.TestCase):
    def test_status_column_fits_every_terminal_state(self) -> None:
        length = Job.__table__.c.status.type.length

        self.assertIsNotNone(length)
        self.assertGreaterEqual(length, len("completed_with_errors"))


if __name__ == "__main__":
    unittest.main()
