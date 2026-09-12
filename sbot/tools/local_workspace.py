import base64
import hashlib
import json
import uuid

from sbot.filenames import safe_filename
from sbot.tools.filesystem import _WorkspaceTool
from sbot.local_workspaces import MAX_BYTES, OfflineError


class LocalWorkspaceTool(_WorkspaceTool):
    name = 'local_workspace'
    wants_progress = True
    description = (
        'Access a user-paired LOCAL folder via its workspace_id, never by a host path. '
        'list lists local folder entries, read reads local UTF-8 text, import copies a local '
        'file into the cloud workspace for document tools and publishes it for Preview/download, '
        'export copies a cloud file to a NEW local filename. Existing local files are never overwritten. '
        'Import files before delegation and pass the resulting cloud paths to teammates. '
        'list, read and import fail immediately when the folder is offline and require the user to reconnect. '
        'An export to an offline folder is QUEUED, not delivered: report it as pending and never say the file was saved. '
        'Never claim success on an error. Maximum file size 8 MB. No local shell execution.'
    )
    parameters = {'type': 'object', 'properties': {
        'workspace_id': {'type': 'string'},
        'action': {'type': 'string', 'enum': ['list', 'read', 'import', 'export']},
        'path': {'type': 'string', 'description': 'Relative LOCAL path; . for root listing'},
        'cloud_path': {'type': 'string', 'description': 'For export: source file in cloud workspace'},
    }, 'required': ['workspace_id', 'action', 'path']}

    def __init__(self, workspace, broker, owner):
        super().__init__(workspace)
        self.broker, self.owner = broker, owner

    async def execute(self, workspace_id, action, path, cloud_path='', progress=None, **kwargs):
        try:
            if action not in ('list', 'read', 'import', 'export'):
                raise ValueError('Unsupported action')
            payload = {'action': 'list' if action == 'list' else 'read', 'path': path}
            digest = relative_source = ''
            if action == 'export':
                source = self._resolve(cloud_path)
                with source.open('rb') as f:
                    data = f.read(MAX_BYTES+1)
                if len(data) > MAX_BYTES:
                    raise ValueError('File exceeds 8 MB limit')
                digest = hashlib.sha256(data).hexdigest()
                # Store the normalised form: a deferred delivery must resolve to
                # this exact file, not re-interpret the model's raw argument.
                relative_source = source.relative_to(self.workspace).as_posix()
                payload = {'action': 'write', 'path': path, 'data': base64.b64encode(data).decode()}
            try:
                result = await self.broker.call(self.owner, workspace_id, payload)
            except OfflineError as offline:
                if action != 'export':
                    return f'Error: {offline}'
                self.broker.enqueue(self.owner, workspace_id, relative_source, digest, path)
                return (f'Queued, NOT delivered: the local folder is offline. {path} will be written automatically '
                        'when the Local Agent reconnects. Tell the user the file is still pending on their computer.')
            if 'error' in result:
                return 'Error: ' + str(result['error'])
            if action in ('read', 'import'):
                data = base64.b64decode(result['data'], validate=True)
                if len(data) > MAX_BYTES:
                    raise ValueError('File exceeds 8 MB limit')
                if action == 'read':
                    return data.decode('utf-8')[:50000]
                relative = f'.deliveries/{uuid.uuid4().hex}/{safe_filename(path.split("/")[-1])}'
                target = self._resolve(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if progress:
                    progress({'kind': 'artifact_published', 'path': relative})
                return f'Imported and published downloadable file: {relative}. Edit a copy, then export it to a new local filename.'
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, OSError, KeyError) as exc:
            return f'Error: {exc}'
