"""
Converts legacy .xls files to .xlsx via real Excel (COM automation), when
Excel is installed on the machine running this app.

Why this exists: some .xls "quotation" exports (from non-Excel generating
tools — see xls_image_extractor.py's module docstring for the byte-level
diagnosis) have their embedded-picture position data truncated or split
across a structure our own binary parser can't fully reconstruct. Real
Excel, on open, resolves/repairs this correctly (verified directly via COM:
it reported 245 correctly-positioned shapes on a file our parser could only
find 57 for). Rather than reverse-engineer Excel's own repair logic, we let
Excel do it: open the .xls, save it back out as .xlsx, and let the already
reliable openpyxl-based extractor (images.py's _xlsx_sheet_images) read the
result — .xlsx has no equivalent size-capped record to hit in the first
place.

Windows + a real Excel installation only. Callers must treat this as
best-effort and fall back to the raw .xls parser when it returns None
(e.g. Excel not installed, running on Linux, or COM failing for any
reason) — never let this become a hard dependency for uploads to work.
"""
import threading

_lock = threading.Lock()  # serialize COM automation — one Excel instance at a time


def convert_xls_to_xlsx(src_path: str, dst_path: str) -> bool:
    """Convert src_path (.xls) to dst_path (.xlsx) using Excel COM automation.
    Returns True on success, False if Excel/COM isn't available or the
    conversion fails for any reason — callers should fall back gracefully."""
    try:
        import win32com.client as wc
        import pythoncom
    except ImportError:
        return False

    with _lock:
        pythoncom.CoInitialize()
        excel = None
        wb = None
        try:
            excel = wc.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            wb = excel.Workbooks.Open(src_path, ReadOnly=True)
            wb.SaveAs(dst_path, FileFormat=51)  # 51 = xlOpenXMLWorkbook (.xlsx)
            return True
        except Exception:
            return False
        finally:
            try:
                if wb is not None:
                    wb.Close(False)
            except Exception:
                pass
            try:
                if excel is not None:
                    excel.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()
