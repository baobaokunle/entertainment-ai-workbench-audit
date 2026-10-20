"""Verify the fixed public CPU engine; no process, download, profile or model request."""
import ctypes,hashlib,json,re
from pathlib import Path,PurePosixPath
from .backup_scratch import reject_links
from .common import AppError

ROOT=Path(__file__).resolve().parents[1]
ENGINE_MANIFEST=ROOT/'dependencies/model-engine/ollama-cpu-0.40.3-manifest.json'
ENGINE_MANIFEST_SHA256='623d8a5b27d1499c7a0b6e03fb08c88c74e724bb233a2b542f6ec0aa31c0e896'
ENGINE_ARCHIVE_SHA256='e5ecff02dfc7cae8ddf6d6b7a7ac5f727ffeedf4491f44ae75ff5b0820dbbbd5'
ENGINE_VERSION='0.40.3'

def engine_error():raise AppError(503,'AI_ENGINE_FILES_UNVERIFIED','固定本机CPU引擎文件未核验；未启动、下载或切换引擎')

def _digest(path):
    hashed=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):hashed.update(block)
    return hashed.hexdigest()

def _inventory(manifest):
    if type(manifest) is not dict or manifest.get('format')!='dfa-ollama-cpu-engine-1' or manifest.get('version')!=ENGINE_VERSION or manifest.get('platform')!='windows-amd64' or manifest.get('archive_sha256')!=ENGINE_ARCHIVE_SHA256:engine_error()
    files=manifest.get('files')
    if type(files) is not list or not 20<=len(files)<=100:engine_error()
    entries={};directories=set();total=0
    for row in files:
        if type(row) is not dict or set(row)!={'file','bytes','sha256'}:engine_error()
        name=row['file']
        if type(name) is not str or not re.fullmatch(r'[A-Za-z0-9_./+\-]{1,160}',name) or name.startswith('/') or any(part in ('.','..','') for part in name.split('/')):engine_error()
        if name!='ollama.exe' and (not name.startswith('lib/ollama/') or '/' in name.removeprefix('lib/ollama/')):engine_error()
        if name.casefold() in entries or type(row['bytes']) is not int or not 0<row['bytes']<=64*1024**2 or type(row['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',row['sha256']):engine_error()
        entries[name.casefold()]=row;total+=row['bytes']
        directories.update(parent.as_posix().casefold() for parent in PurePosixPath(name).parents if parent.as_posix()!='.')
    if 'ollama.exe' not in entries or total>256*1024**2:engine_error()
    return entries,directories

def _verify_files(root,manifest):
    entries,directories=_inventory(manifest)
    reject_links(root)
    if not root.is_dir():engine_error()
    actual=set();actual_directories=set();pending=[root];visited=0
    while pending:
        directory=pending.pop()
        for item in directory.iterdir():
            visited+=1
            if visited>120:engine_error()
            reject_links(item)
            name=item.relative_to(root).as_posix().casefold()
            if item.is_dir():
                if name not in directories or name in actual_directories:engine_error()
                actual_directories.add(name)
                pending.append(item)
            elif item.is_file():
                if name not in entries or name in actual:engine_error()
                actual.add(name)
            else:engine_error()
    if actual!=set(entries):engine_error()
    for row in entries.values():
        file=root/row['file'];reject_links(file)
        if file.stat().st_size!=row['bytes'] or _digest(file)!=row['sha256']:engine_error()
    return {'engine_version':ENGINE_VERSION,'manifest_sha256':ENGINE_MANIFEST_SHA256,'verified_files':len(entries),
        'verified_bytes':sum(row['bytes'] for row in entries.values()),'program':root/'ollama.exe',
        'engine_started':False,'model_requested':False,'private_profile_read':False}

def verify_engine_directory(engine_root):
    try:
        root=Path(engine_root)
        if not root.is_absolute() or root.drive.upper()!='F:':engine_error()
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetDriveTypeW.argtypes=[ctypes.c_wchar_p];kernel.GetDriveTypeW.restype=ctypes.c_uint32
        if kernel.GetDriveTypeW(root.anchor)!=3:engine_error()
        reject_links(ENGINE_MANIFEST)
        with ENGINE_MANIFEST.open('rb') as stream:raw=stream.read(65537)
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=ENGINE_MANIFEST_SHA256:engine_error()
        return _verify_files(root,json.loads(raw))
    except AppError:raise
    except (OSError,ValueError,TypeError,RecursionError,AttributeError):engine_error()
