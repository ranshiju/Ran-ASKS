"""Project moves preserve immutable artifacts and fail closed at path boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_paths import project_path, repo_path
import presentation_state as presentation
import science_exhibit as exhibit
import workspace_state as workspace
import research_memory
import source_ingest


class ProjectMoves(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.projects = self.root / 'projects'
        self.projects.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def mapping(self, moves):
        (self.projects / '.path-migrations.json').write_text(json.dumps({
            'schema': 'project-path-migrations-v1', 'moves': moves}))

    def test_missing_map_and_existing_old_root_keep_original_path(self):
        self.assertEqual(repo_path(self.root, 'projects/demo/new.txt'), self.projects / 'demo/new.txt')
        (self.projects / 'demo').mkdir()
        self.mapping({'demo': 'group/demo'})
        self.assertEqual(project_path(self.projects, 'demo/new.txt'), self.projects / 'demo/new.txt')

    def test_moved_paths_work_across_workspace_legacy_and_managed_readers(self):
        target = self.projects / 'group/demo'
        target.mkdir(parents=True)
        (target/'source.txt').write_text('unchanged')
        self.mapping({'demo':'group/demo'})
        with patch.object(workspace,'PROJECTS_DIR',self.projects), patch.object(research_memory,'PROJECTS_DIR',self.projects):
            self.assertEqual(workspace.resolve_project('demo'),target)
            self.assertEqual(research_memory.project_root('demo'),target)
            self.assertEqual(workspace.resolve_project(self.projects/'demo'),target)
        self.assertEqual(source_ingest.managed(self.root,'projects/demo/source.txt'),target/'source.txt')
        self.assertEqual(repo_path(self.root,'academic/raw/source.txt'),self.root/'academic/raw/source.txt')

    def test_corrupt_chained_overlapping_and_escaping_maps_fail(self):
        for moves in [{'demo':'../outside'}, {'demo':'demo/child'},
                      {'demo':'other','other':'group/other'},
                      {'demo':'group/demo','demo/child':'group/child'}]:
            self.mapping(moves)
            with self.assertRaises(ValueError):project_path(self.projects,'demo/file')
        self.mapping({'demo':'group/demo'})
        with self.assertRaises(ValueError):project_path(self.projects,'demo/file')
        with self.assertRaises(ValueError):project_path(self.projects,'../private/file')

    def test_symlink_roots_targets_and_suffixes_are_rejected(self):
        target=self.projects/'group/demo';target.mkdir(parents=True)
        self.mapping({'demo':'group/demo'})
        (target/'escape').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):project_path(self.projects,'demo/escape/file')
        (self.projects/'demo').symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):project_path(self.projects,'demo/file')
        (self.projects/'demo').unlink()
        (self.projects/'.path-migrations.json').unlink()
        (self.projects/'.path-migrations.json').symlink_to(self.root/'absent')
        with self.assertRaises(ValueError):project_path(self.projects,'demo/file')

    def test_presentation_store_move_preserves_pointer_and_revision(self):
        old=self.projects/'demo';old.mkdir()
        store=presentation.Store(old/'presentations/deck',self.root)
        store.initialize(presentation.example_plan())
        before={p.relative_to(old).as_posix():p.read_bytes() for p in old.rglob('*') if p.is_file()}
        target=self.projects/'group/demo';target.parent.mkdir();old.rename(target)
        self.mapping({'demo':'group/demo'})
        moved=presentation.Store(target/'presentations/deck',self.root)
        legacy=presentation.Store(old/'presentations/deck',self.root)
        self.assertEqual(moved.read(),legacy.read())
        self.assertEqual(before,{p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError):presentation.Store(self.root/'private/deck',self.root)
        with self.assertRaises(ValueError):presentation.Store(target/'presentations/../bad',self.root)

    def test_exhibit_store_move_preserves_revision_and_allows_new_nested_store(self):
        old=self.projects/'demo';old.mkdir()
        with patch.object(exhibit,'REPO',self.root):
            exhibit.init_store(old/'exhibits/board')
            original=exhibit.load_state(old/'exhibits/board')
            before={p.relative_to(old).as_posix():p.read_bytes() for p in old.rglob('*') if p.is_file()}
            target=self.projects/'group/demo';target.parent.mkdir();old.rename(target)
            self.mapping({'demo':'group/demo'})
            self.assertEqual(exhibit.load_state(target/'exhibits/board'),original)
            self.assertEqual(exhibit.load_state(old/'exhibits/board'),original)
            self.assertEqual(before,{p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()})
            exhibit.init_store(target/'exhibits/new-board')
            self.assertEqual(exhibit.load_state(target/'exhibits/new-board')['revision'],0)


if __name__=='__main__':unittest.main()
