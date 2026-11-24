"""One packaged skill source for native Harness discovery and the console viewer.

Each robot connection freezes validated SKILL.md files. The Harness reads a private
snapshot of those exact files so UI text and model instructions cannot drift during
a turn. Add skills to the package directory, not to a separate Python/UI registry.
"""
from pathlib import Path
import re
import tempfile

import yaml

from steward.net import HttpError

SKILL_DIR = Path(__file__).resolve().parents[5] / 'dsh/packages/dsh-bundle-ego-real/skills'


class SkillCatalog:
    """Validate at robot connection time; release the private snapshot after agent exit."""

    def __init__(self, source=SKILL_DIR):
        self.entries = {}
        self._snapshot = None
        if not source.is_dir():
            raise ValueError(f'真机技能目录不存在: {source}')
        for path in sorted(source.glob('*/SKILL.md')):
            text = path.read_text(encoding='utf-8')
            parts = re.fullmatch(r'---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)', text, re.S)
            if parts is None:
                raise ValueError(f'技能缺少 YAML frontmatter: {path.name}')
            meta = yaml.safe_load(parts[1])
            if not isinstance(meta, dict):
                raise ValueError(f'技能元信息无效: {path.parent.name}')
            name, description = meta.get('name'), meta.get('description')
            if (not isinstance(name, str) or len(name) > 64 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
                    or name != path.parent.name or name in self.entries
                    or not isinstance(description, str) or not description.strip() or not parts[2].strip()):
                raise ValueError(f'技能名称、描述或正文无效: {path.parent.name}')
            # Console skills are model-callable instructions. Reject policy mismatches
            # rather than advertising an entry the native skill tool cannot load.
            if meta.get('disable-model-invocation', False) is not False:
                raise ValueError(f'真机技能必须允许模型加载: {name}')
            self.entries[name] = {'name': name, 'description': description, 'text': text}

    def summaries(self):
        """Return only discovery metadata; full instruction text is fetched on expansion."""
        return [{key: entry[key] for key in ('name', 'description')} for entry in self.entries.values()]

    def detail(self, name):
        """Lookup a registered name, never a caller-supplied filesystem path."""
        if name not in self.entries:
            raise HttpError(404, '未知的真机 skill')
        return dict(self.entries[name])

    @property
    def directory(self):
        """Materialize once for native skill-filesystem; no default host skill roots are scanned."""
        if self._snapshot is None:
            snapshot = tempfile.TemporaryDirectory(prefix='steward-real-skills-')
            try:
                for name, entry in self.entries.items():
                    folder = Path(snapshot.name) / name
                    folder.mkdir()
                    (folder / 'SKILL.md').write_text(entry['text'], encoding='utf-8')
            except Exception:
                snapshot.cleanup()
                raise
            self._snapshot = snapshot
        return self._snapshot.name

    def close(self):
        """Remove the snapshot only after the Harness process has stopped reading it."""
        if self._snapshot is not None:
            self._snapshot.cleanup()
            self._snapshot = None
