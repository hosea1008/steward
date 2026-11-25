"""One packaged skill source for native Harness discovery and the console viewer.

Each robot connection snapshots validated SKILL.md files; idle saves replace the snapshot. The Harness reads a private
snapshot of those exact files so UI text and model instructions cannot drift during
a turn. Add skills to the package directory, not to a separate Python/UI registry.
"""
import hashlib
import os
from pathlib import Path
import re
import tempfile

import yaml

from steward.net import HttpError

SKILL_DIR = Path(__file__).resolve().parents[5] / 'dsh/packages/dsh-bundle-ego-real/skills'


class SkillCatalog:
    """Validate on connection and save; release private snapshots only while the agent is idle."""

    def __init__(self, source=SKILL_DIR):
        self.entries = {}
        self._snapshot = None
        if not source.is_dir():
            raise ValueError(f'真机技能目录不存在: {source}')
        self.source = source.resolve()
        for path in sorted(source.glob('*/SKILL.md')):
            entry = self._parse(path.parent.name, path.read_text(encoding='utf-8'))
            self.entries[entry['name']] = entry

    @staticmethod
    def _parse(expected_name, text):
        parts = re.fullmatch(r'---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)', text, re.S)
        if parts is None:
            raise ValueError('技能缺少 YAML frontmatter')
        try:
            meta = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            raise ValueError('技能 YAML 格式无效') from exc
        if not isinstance(meta, dict):
            raise ValueError('技能元信息无效')
        name, description = meta.get('name'), meta.get('description')
        if (not isinstance(name, str) or len(name) > 64 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
                or name != expected_name or not isinstance(description, str)
                or not description.strip() or not parts[2].strip()):
            raise ValueError('技能名称必须与目录相同，描述和正文不能为空')
        if meta.get('disable-model-invocation', False) is not False:
            raise ValueError('真机技能必须允许模型加载')
        return dict(name=name, description=description, text=text,
                    revision=hashlib.sha256(text.encode('utf-8')).hexdigest())

    def save(self, name, text, previous):
        """Persist an existing skill with atomic replacement; caller excludes active turns.

        Compare both the connection snapshot and disk text so a stale editor cannot
        overwrite a newer save or an out-of-band edit. Never accept filesystem paths.
        """
        current = self.detail(name)
        if (not isinstance(text, str) or len(text) > 64000
                or not isinstance(previous, str) or len(previous) > 64000):
            raise HttpError(400, 'skill 文本最多 64000 字符')
        text = text.rstrip() + '\n'
        try:
            entry = self._parse(name, text)
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        path = self.source / name / 'SKILL.md'
        if path.resolve().parent != (self.source / name) or path.parent.resolve().parent != self.source:
            raise HttpError(400, '不能写入技能目录之外的文件或符号链接')
        if previous != current['text'] or path.read_text(encoding='utf-8') != previous:
            raise HttpError(409, 'skill 已被修改，请重新载入核对；若磁盘由外部修改，请重新连接真机配置')
        fd, filename = tempfile.mkstemp(dir=path.parent, prefix='.skill-')
        temporary = Path(filename)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
            temporary.chmod(path.stat().st_mode & 0o777)
            # No turn may read this snapshot while editing. Rebuild from the published
            # entries on the next request, including when replacing the disk file fails.
            self.close()
            temporary.replace(path)
            self.entries[name] = entry
        finally:
            temporary.unlink(missing_ok=True)
        return dict(entry)

    def summaries(self):
        """Return only discovery metadata; full instruction text is fetched on expansion."""
        return [{key: entry[key] for key in ('name', 'description', 'revision')} for entry in self.entries.values()]

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
