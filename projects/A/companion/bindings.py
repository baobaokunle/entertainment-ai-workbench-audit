"""Explicit executable candidates; a name or digest alone does not prove a work window."""
import hashlib,re
from pathlib import Path

CANDIDATES={'editing_product':'剪映','browser_product':'Google Chrome','editing_executables':(),'browser_hwnd_binding':'unverified'}

class EditingRegistry:
    def __init__(self,entries=(),*,hwnd_verifier=None):
        if not isinstance(entries,(list,tuple)) or len(entries)>16:raise ValueError('Finite explicit executable registry required')
        self.entries=[];self.verifier=hwnd_verifier;self.cache={}
        for entry in entries:
            if not isinstance(entry,dict) or set(entry)!={'image_path','program_sha256','evidence_id'} or not isinstance(entry['image_path'],str) or not Path(entry['image_path']).is_absolute() or not isinstance(entry['program_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',entry['program_sha256']) or not isinstance(entry['evidence_id'],str) or not 1<=len(entry['evidence_id'])<=200:raise ValueError('Exact image path, digest and independently verified evidence required')
            self.entries.append(dict(entry))
    def probe(self,context):
        if not self.verifier or not context.get('image_path') or not context.get('foreground_hwnd'):return None
        path=Path(context['image_path'])
        for entry in self.entries:
            if path!=Path(entry['image_path']):continue
            try:
                if path.is_symlink() or getattr(path,'is_junction',lambda:False)():return None
                stat=path.stat();key=(str(path),stat.st_mtime_ns,stat.st_size)
                if stat.st_size>1024**3:return None
                checksum=self.cache.get(key)
                if checksum is None:
                    with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
                    self.cache={key:checksum}
            except OSError:return None
            if checksum!=entry['program_sha256'] or self.verifier(context,dict(entry)) is not True:return None
            return {'category':'editing','binding_verified':True,'program_sha256':checksum,'binding_evidence_id':entry['evidence_id'],'active_tab':None,'url_verified':None,'browser_window_id':None}
        return None
