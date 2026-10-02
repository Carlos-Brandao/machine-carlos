"""Small in-memory importer fixture; PostgreSQL locking is tested separately."""

from machine_admin.models import Dataset, DatasetMembership, DatasetRecord


class ResultRows(list):
    def all(self):
        return list(self)


class DatasetImportQueries:
    def _matching_records(self, model, statement):
        values = [value for value in self.records if isinstance(value, model)]
        for condition in statement._where_criteria:
            values = [value for value in values if condition.operator(
                getattr(value, condition.left.key), condition.right.value
            )]
        return values

    def scalar(self, statement):
        column = statement.column_descriptions[0]
        model = column.get("entity")
        if model is Dataset:
            values = self._matching_records(model, statement)
            return values[0] if values else None
        if model is DatasetRecord and column["name"] == "coalesce":
            return max((row.row_number for row in self._matching_records(model, statement)), default=1)
        raise AssertionError(f"Unexpected scalar query in importer fixture: {statement}")

    def execute(self, statement):
        columns = statement.column_descriptions
        if columns[0]["name"] == "pg_advisory_xact_lock":
            return ResultRows()
        if columns[0].get("entity") is DatasetMembership:
            return ResultRows((row.identity_key, row.dataset_record_id)
                              for row in self._matching_records(DatasetMembership, statement))
        raise AssertionError(f"Unexpected row query in importer fixture: {statement}")

    def assign_record_id(self, value):
        if isinstance(value, (Dataset, DatasetRecord, DatasetMembership)) and value.id is None:
            value.id = 1 + max((row.id for row in self.records if isinstance(row, type(value))), default=0)
