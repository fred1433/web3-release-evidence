"""Copy the recorded reports, their screenshots and the variants diff into site/data for the page."""
import os, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src, dst = os.path.join(ROOT, 'reports'), os.path.join(ROOT, 'site', 'data')
os.makedirs(os.path.join(dst, 'evidence'), exist_ok=True)
for f in ('faulty.json', 'corrected.json', 'corrected-skip-C08.json', 'stability-faulty.json', 'stability-corrected.json', 'mutants.json'):
    shutil.copy(os.path.join(src, f), os.path.join(dst, f))
for v in ('faulty', 'corrected'):
    shutil.copytree(os.path.join(src, 'evidence', v), os.path.join(dst, 'evidence', v), dirs_exist_ok=True)
shutil.copy(os.path.join(ROOT, 'variants.diff'), os.path.join(dst, 'variants.diff'))
print('site/data updated')
