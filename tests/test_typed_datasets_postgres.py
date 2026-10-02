"""Typed base acceptance against isolated PostgreSQL; no portal or captcha calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from machine_admin.config import Settings
from machine_admin.datasets import archive_dataset, create_job_for_dataset, import_dataset, normalize_cpf, update_dataset
from machine_admin.models import AdminUser, Base, Dataset, DatasetMembership, DatasetRecord, Job, JobItem, Municipality, Platform, Schedule
from machine_admin.security import SecretCipher, fingerprint_identifier


def cpf_for(number):
    digits = [int(d) for d in f'{number:09d}']
    for start in (10, 11):
        remainder = sum(n * weight for n, weight in zip(digits, range(start, 1, -1))) % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return ''.join(map(str, digits))


def csv_for(*numbers, registrations=None):
    lines = ['CPF,MATRICULA']
    lines += [f'{cpf_for(n)},{registrations[index] if registrations else n}' for index, n in enumerate(numbers)]
    return ('\n'.join(lines) + '\n').encode()


@unittest.skipUnless(os.getenv('MACHINE_TEST_DATABASE_URL'), 'isolated PostgreSQL URL not configured')
class TypedDatasetAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ['MACHINE_TEST_DATABASE_URL'])
        if url.get_backend_name() != 'postgresql' or not (url.database or '').startswith('machine_acceptance_'):
            raise RuntimeError('Refusing non-isolated database')
        cls.schema = 'typed_' + secrets.token_hex(8)
        cls.raw_engine = create_engine(url, pool_size=4)
        with cls.raw_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = cls.raw_engine.execution_options(schema_translate_map={None: cls.schema})
        cls.factory = sessionmaker(bind=cls.engine, expire_on_commit=False)
        Base.metadata.create_all(cls.engine)
        cls.storage = tempfile.TemporaryDirectory()
        cls.settings = Settings(str(url), 's'*48, b'k'*32, False, ('testserver',), Path(cls.storage.name), 1048576, None, None)

    @classmethod
    def tearDownClass(cls):
        with cls.raw_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.raw_engine.dispose()
        cls.storage.cleanup()

    def setUp(self):
        self.slug = 'test-' + secrets.token_hex(6)
        with self.factory() as session, session.begin():
            user = AdminUser(email=f'{self.slug}@invalid.test', display_name='Synthetic', password_hash='unused', role='admin')
            session.add(user)
            if not session.get(Platform, 'rf1'):
                session.add(Platform(slug='rf1', name='RF1', runner='rf1'))
            session.flush()
            self.user_id = user.id
            session.add(Municipality(slug=self.slug, name='Synthetic', platform_slug='rf1', max_workers=1,
                operational_status='ready', input_schema={'required':['cpf'], 'deduplication_key':['cpf']}))

    def upload(self, kind, numbers, name='Base original', registrations=None):
        with self.factory() as session, session.begin():
            dataset = import_dataset(session, self.settings, municipality_slug=self.slug, filename='synthetic.csv',
                payload=csv_for(*numbers, registrations=registrations), uploaded_by_id=self.user_id,
                display_name=name, dataset_type=kind)
            return dataset.id

    def bases(self):
        with self.factory() as session:
            return {d.dataset_type:(d.id,d.row_count,d.display_name) for d in session.scalars(select(Dataset).where(
                Dataset.municipality_slug == self.slug, Dataset.status != 'archived'))}

    def test_reimport_adds_only_excess_preserves_name_and_updates_general(self):
        original = self.upload('efetivos', [123456789,234567890,345678901])
        self.assertEqual(original, self.upload('efetivos', [123456789,234567890,345678901,456789012], 'Outro nome'))
        bases = self.bases()
        self.assertEqual((original,4,'Base original'), bases['efetivos'])
        self.assertEqual(4,bases['geral'][1])
        with self.factory() as session:
            self.assertEqual(4,session.scalar(select(func.count()).select_from(DatasetMembership).where(DatasetMembership.dataset_id==original)))

    def test_general_is_union_including_direct_import_and_is_idempotent(self):
        general = self.upload('geral', [123456789,234567890])
        self.upload('efetivos', [234567890,345678901])
        self.upload('temporarios', [345678901,456789012])
        self.upload('comissionados', [123456789,567890123])
        self.assertEqual(5,self.bases()['geral'][1])
        self.assertEqual(general,self.upload('geral',[123456789,567890123]))
        self.assertEqual(5,self.bases()['geral'][1])

    def test_same_cpf_different_registration_obeys_agreement_identity(self):
        with self.factory() as session, session.begin():
            session.get(Municipality,self.slug).input_schema={'required':['cpf','registration'], 'deduplication_key':['cpf','registration']}
        self.upload('efetivos',[123456789,123456789],registrations=['A','B'])
        self.upload('efetivos',[123456789,123456789],registrations=['a','C'])
        self.assertEqual(3,self.bases()['efetivos'][1])
        self.assertEqual(3,self.bases()['geral'][1])

    def test_existing_job_snapshot_unchanged_after_append_and_archive(self):
        identity=self.upload('efetivos',[123456789,234567890])
        with self.factory() as session, session.begin():
            job=create_job_for_dataset(session,dataset=session.get(Dataset,identity),requested_by_id=self.user_id)
            job_id=job.id
            old_ids=set(session.scalars(select(JobItem.dataset_record_id).where(JobItem.job_id==job_id)))
        self.upload('efetivos',[345678901])
        with self.factory() as session, session.begin():
            self.assertEqual(2,session.get(Job,job_id).total_items)
            self.assertEqual(old_ids,set(session.scalars(select(JobItem.dataset_record_id).where(JobItem.job_id==job_id))))
            archive_dataset(session,dataset=session.get(Dataset,identity))
            self.assertEqual(old_ids,set(session.scalars(select(JobItem.dataset_record_id).where(JobItem.job_id==job_id))))
            self.assertEqual(2,session.scalar(select(func.count()).select_from(DatasetRecord).where(DatasetRecord.id.in_(old_ids))))
        self.assertEqual(3,self.bases()['geral'][1])

    def test_concurrent_uploads_cannot_duplicate_catalog_or_members(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda nums:self.upload('efetivos',nums),[[123456789,234567890],[234567890,345678901]]))
        self.assertEqual(results[0],results[1])
        self.assertEqual(2,len(self.bases()))
        self.assertEqual(3,self.bases()['efetivos'][1])
        self.assertEqual(3,self.bases()['geral'][1])

    def test_rename_reclassify_and_delete_disable_schedules(self):
        identity=self.upload('efetivos',[123456789])
        with self.factory() as session, session.begin():
            dataset=session.get(Dataset,identity)
            update_dataset(session,dataset=dataset,display_name='Nome novo',dataset_type='temporarios')
            schedule=Schedule(name='Synthetic',dataset_id=identity,cron_expression='0 9 * * *',
                timezone='America/Fortaleza',next_run_at=datetime.now(UTC)+timedelta(days=1),enabled=True)
            session.add(schedule)
            session.flush()
            archive_dataset(session,dataset=dataset)
            session.flush()
            session.refresh(schedule)
            self.assertFalse(schedule.enabled)
            self.assertEqual('Nome novo',dataset.display_name)
        self.assertEqual({'geral'},set(self.bases()))

    def test_general_cannot_be_removed_while_specific_base_is_active(self):
        self.upload('efetivos',[123456789])
        with self.factory() as session, session.begin():
            general=session.scalar(select(Dataset).where(Dataset.municipality_slug==self.slug,Dataset.dataset_type=='geral'))
            with self.assertRaises(ValueError):
                archive_dataset(session,dataset=general)

    def test_classifying_legacy_deduplicates_catalog_and_preserves_historical_job(self):
        cipher = SecretCipher(self.settings.master_key)
        with self.factory() as session, session.begin():
            legacy = Dataset(
                municipality_slug=self.slug, uploaded_by_id=self.user_id, display_name='Legacy',
                dataset_type=None, original_filename='legacy.csv', storage_path='generated',
                sha256='0' * 64, row_count=3, status='ready', duplicate_policy='keep_all',
            )
            session.add(legacy)
            session.flush()
            dataset_id = legacy.id
            records = []
            for row_number, number in enumerate([123456789, 123456789, 234567890], start=2):
                cpf = cpf_for(number)
                context = secrets.token_hex(16)
                record = DatasetRecord(
                    dataset_id=dataset_id, row_number=row_number, encryption_context=context,
                    cpf_ciphertext=cipher.encrypt(cpf, context=f'record:{context}:cpf'),
                    cpf_fingerprint=fingerprint_identifier(self.settings.master_key, cpf),
                    cpf_last4=cpf[-4:], registration=str(row_number),
                    source_ciphertext=cipher.encrypt(json.dumps({'CPF': cpf, 'MATRICULA': str(row_number)}), context=f'record:{context}:source'),
                    source_data={'columns': ['CPF', 'MATRICULA']},
                )
                session.add(record)
                session.flush()
                session.add(DatasetMembership(
                    dataset_id=dataset_id, dataset_record_id=record.id, identity_key=f'legacy-record:{record.id}',
                ))
                records.append(record)
            session.flush()
            originals = {record.id: (record.cpf_ciphertext, record.source_ciphertext) for record in records}
            job = create_job_for_dataset(session, dataset=legacy, requested_by_id=self.user_id)
            job_id = job.id
            self.assertEqual(3, job.total_items)

        with self.factory() as session, session.begin():
            legacy = session.get(Dataset, dataset_id)
            update_dataset(session, dataset=legacy, display_name='Efetivos classificados', dataset_type='efetivos')
            self.assertEqual(2, legacy.row_count)
            self.assertEqual(3, legacy.metadata_json['rows_before_classification'])
            self.assertEqual(3, session.get(Job, job_id).total_items)
            self.assertEqual(set(originals), set(session.scalars(
                select(JobItem.dataset_record_id).where(JobItem.job_id == job_id)
            )))
            remaining = session.scalars(select(DatasetRecord).where(DatasetRecord.dataset_id == dataset_id))
            self.assertEqual(originals, {record.id: (record.cpf_ciphertext, record.source_ciphertext) for record in remaining})
            new_job = create_job_for_dataset(session, dataset=legacy, requested_by_id=self.user_id)
            self.assertEqual(2, new_job.total_items)
        self.assertEqual(2, self.bases()['efetivos'][1])
        self.assertEqual(2, self.bases()['geral'][1])

    def test_classifying_general_as_specific_rebuilds_general_union(self):
        original_general = self.upload('geral', [123456789, 234567890], 'Geral original')
        self.upload('temporarios', [345678901])
        with self.factory() as session, session.begin():
            before = set(session.scalars(select(DatasetMembership.dataset_record_id).where(
                DatasetMembership.dataset_id == original_general
            )))
            self.assertEqual(3, len(before))
            update_dataset(session, dataset=session.get(Dataset, original_general),
                display_name='Agora Efetivos', dataset_type='efetivos')
            new_general = session.scalar(select(Dataset).where(
                Dataset.municipality_slug == self.slug, Dataset.dataset_type == 'geral', Dataset.status == 'ready'
            ))
            self.assertNotEqual(original_general, new_general.id)
            self.assertEqual(3, new_general.row_count)
            self.assertEqual(before, set(session.scalars(select(DatasetMembership.dataset_record_id).where(
                DatasetMembership.dataset_id == new_general.id
            ))))
        self.assertEqual(original_general, self.bases()['efetivos'][0])
        self.upload('geral', [456789012])
        self.assertEqual(4, self.bases()['geral'][1])
        self.assertEqual(3, self.bases()['efetivos'][1])
        self.upload('efetivos', [567890123])
        self.assertEqual(5, self.bases()['geral'][1])
        self.assertEqual(4, self.bases()['efetivos'][1])

    def test_failed_append_keeps_existing_storage_and_members(self):
        identity=self.upload('geral',[123456789])
        with self.factory() as session:
            before=session.get(Dataset,identity)
            storage=Path(before.storage_path)
            file_before=storage.read_bytes()
        with self.factory() as session:
            with self.assertRaises(ValueError):
                import_dataset(session,self.settings,municipality_slug=self.slug,filename='bad.csv',
                    payload=b'CPF\n11111111111\n',uploaded_by_id=self.user_id,display_name='Invalid',dataset_type='geral')
            session.rollback()
        self.assertEqual(file_before,storage.read_bytes())
        self.assertEqual(1,self.bases()['geral'][1])

    def test_input_rule_change_while_parsing_rejects_before_catalog_or_blob_writes(self):
        previous_files = set(self.settings.storage_dir.rglob('*.enc'))
        rule_changed = False

        def normalize_during_rule_change(value):
            nonlocal rule_changed
            if not rule_changed:
                rule_changed = True
                with self.factory() as session, session.begin():
                    session.get(Municipality, self.slug).input_schema = {
                        'required': ['cpf', 'registration'],
                        'deduplication_key': ['cpf', 'registration'],
                    }
            return normalize_cpf(value)

        with patch('machine_admin.datasets.normalize_cpf', side_effect=normalize_during_rule_change):
            with self.assertRaisesRegex(ValueError, 'mudaram durante a importação'):
                self.upload('efetivos', [123456789])
        self.assertEqual({}, self.bases())
        self.assertEqual(previous_files, set(self.settings.storage_dir.rglob('*.enc')))
        # A fresh request validates against the new rule and succeeds normally.
        self.upload('efetivos', [123456789])
        self.assertEqual(1, self.bases()['efetivos'][1])
