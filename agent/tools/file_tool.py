"""Files domain — file_index (every file in the BMS/IMS/FDS document libraries, names/paths only)."""

from ._base import BaseTool


class FileTool(BaseTool):
    name = "files"

    def find_files(self, keyword, library=None, ext=None, site=None, user_role="default"):
        """Filename/path search across all BMS document libraries. Multi-word keywords
        match ALL words (any order) against name + folder path, so 'veteran logo'
        finds 'Veterans Employment Supporter logo.png' in a Logos folder."""
        words = [w for w in str(keyword or "").split() if w][:6]
        w = [f"(file_name ILIKE '%{self.esc(t)}%' OR folder_path ILIKE '%{self.esc(t)}%')" for t in words]
        if site:
            w.append(f"site ILIKE '%{self.esc(site)}%'")
        if library:
            w.append(f"library ILIKE '%{self.esc(library)}%'")
        if ext:
            w.append(f"ext = '{self.esc(str(ext).lstrip('.').lower())}'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT site, library, file_name, folder_path, ext, size_kb, modified_at, modified_by, web_url "
               f"FROM file_index{where} ORDER BY modified_at DESC LIMIT 50")
        return self._query("find_files", {"keyword": keyword, "library": library, "ext": ext, "site": site},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} file(s) match"
                           + (f". e.g. {tr.data[0]['file_name']} — {tr.data[0]['site']}/{tr.data[0]['library']}/"
                              f"{tr.data[0]['folder_path']}" if tr.data else "."))

    def list_folder(self, folder, library=None, site=None, user_role="default"):
        """Everything in one folder (path keyword), newest first."""
        w = [f"folder_path ILIKE '%{self.esc(folder)}%'"]
        if site:
            w.append(f"site ILIKE '%{self.esc(site)}%'")
        if library:
            w.append(f"library ILIKE '%{self.esc(library)}%'")
        sql = ("SELECT site, library, file_name, folder_path, ext, size_kb, modified_at, modified_by, web_url "
               f"FROM file_index WHERE {' AND '.join(w)} ORDER BY modified_at DESC LIMIT 100")
        return self._query("list_folder", {"folder": folder, "library": library, "site": site}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} file(s) in folders matching '{folder}'.")
