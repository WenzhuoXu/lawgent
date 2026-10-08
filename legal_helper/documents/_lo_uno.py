"""LibreOffice compare / accept-all / reject-all, driven over UNO.

Run by a Python that has LibreOffice's ``uno`` module (the system python3 on
Debian/Ubuntu, not the conda env), always as ``python3 -I _lo_uno.py …`` so
nothing in this package directory shadows the standard library. It imports
nothing from ``legal_helper``.

    compare OLD NEW OUT [--author NAME]   differences as native tracked changes
    accept  IN OUT                        accept every tracked change
    reject  IN OUT                        reject every tracked change

Each call starts its own ``soffice`` under a throwaway profile and a private
pipe, so concurrent calls never share a profile lock (the failure mode that
lost 2 of 4 concurrent renders), and terminates it on the way out. The same
dispatch is what ``unoserver``'s ``unocompare`` (MIT) issues; running it
directly avoids a second server process and a dependency in the system
Python.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


def _prop(name, value):
    from com.sun.star.beans import PropertyValue

    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def _url(path):
    import uno

    return uno.systemPathToFileUrl(os.path.abspath(path))


class Office:
    def __init__(self, soffice, timeout=60.0):
        import uno

        self.profile = tempfile.mkdtemp(prefix="lh-uno-profile-")
        self.pipe = "lh_" + uuid.uuid4().hex
        self.proc = subprocess.Popen(
            [
                soffice,
                "-env:UserInstallation=" + _url(self.profile),
                "--headless",
                "--invisible",
                "--nologo",
                "--norestore",
                "--nodefault",
                "--nolockcheck",
                "--accept=pipe,name=%s;urp;StarOffice.ComponentContext" % self.pipe,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        deadline = time.time() + timeout
        while True:
            try:
                self.ctx = resolver.resolve("uno:pipe,name=%s;urp;StarOffice.ComponentContext" % self.pipe)
                break
            except Exception:  # noqa: BLE001 — not listening yet
                if self.proc.poll() is not None:
                    raise RuntimeError("soffice exited before accepting a connection")
                if time.time() > deadline:
                    raise RuntimeError("soffice did not accept a UNO connection in time")
                time.sleep(0.25)
        self.smgr = self.ctx.ServiceManager
        self.desktop = self.smgr.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)

    def set_author(self, name):
        provider = self.smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", self.ctx)
        access = provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationUpdateAccess",
            (_prop("nodepath", "/org.openoffice.UserProfile/Data"),),
        )
        access.setPropertyValue("givenname", name)
        access.setPropertyValue("sn", "")
        access.setPropertyValue("initials", "")
        access.commitChanges()

    @staticmethod
    def stamp(doc, author):
        from com.sun.star.util import DateTime

        now = time.gmtime()
        stamp = DateTime()
        stamp.Year, stamp.Month, stamp.Day = now.tm_year, now.tm_mon, now.tm_mday
        stamp.Hours, stamp.Minutes, stamp.Seconds = now.tm_hour, now.tm_min, now.tm_sec
        stamp.IsUTC = True
        props = doc.getDocumentProperties()
        props.Author = author
        props.ModifiedBy = author
        props.ModificationDate = stamp

    def load(self, path):
        doc = self.desktop.loadComponentFromURL(_url(path), "_blank", 0, (_prop("Hidden", True),))
        if doc is None:
            raise RuntimeError("LibreOffice could not open %s" % path)
        return doc

    def dispatch(self, doc, command, args=()):
        helper = self.smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.ctx)
        helper.executeDispatch(doc.getCurrentController().getFrame(), command, "", 0, tuple(args))

    @staticmethod
    def store_docx(doc, out):
        doc.storeToURL(_url(out), (_prop("FilterName", "MS Word 2007 XML"), _prop("Overwrite", True)))

    def close(self):
        try:
            self.desktop.terminate()
        except Exception:  # noqa: BLE001 — the bridge drops as soffice exits
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        shutil.rmtree(self.profile, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["compare", "accept", "reject"])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--soffice", default=shutil.which("soffice") or "soffice")
    parser.add_argument("--author", default="")
    args = parser.parse_args(argv)
    office = Office(args.soffice)
    try:
        if args.author:
            office.set_author(args.author)
        if args.command == "compare":
            old, new, out = args.paths
            doc = office.load(new)
            try:
                if args.author:
                    # Compare attributes every difference to the revised
                    # document's last author and modification time, not to
                    # the user profile; stamp both so the marks carry the
                    # requested author and today's date.
                    office.stamp(doc, args.author)
                office.dispatch(doc, ".uno:CompareDocuments", (_prop("URL", _url(old)), _prop("NoAcceptDialog", True)))
                office.store_docx(doc, out)
            finally:
                doc.close(True)
        else:
            src, out = args.paths
            doc = office.load(src)
            try:
                command = ".uno:AcceptAllTrackedChanges" if args.command == "accept" else ".uno:RejectAllTrackedChanges"
                office.dispatch(doc, command)
                office.store_docx(doc, out)
            finally:
                doc.close(True)
    finally:
        office.close()
    if not os.path.isfile(args.paths[-1]):
        print("no output written", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
