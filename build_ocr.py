"""Build the local macOS helper with the Apple command line tools."""
from pathlib import Path
import subprocess
import sys

if sys.platform != 'darwin':
    raise SystemExit('Модуль локального распознавания предназначен для macOS.')
root=Path(__file__).parent
subprocess.run(['clang','-fobjc-arc','-framework','Foundation','-framework','Vision',
                '-framework','ImageIO','-framework','CoreGraphics',
                str(root/'native/invoice_ocr.m'),'-o',str(root/'native/invoice-ocr')],check=True)
print('Модуль распознавания готов.')
