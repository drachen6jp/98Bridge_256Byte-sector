"""
Plugin Manager dialog for pc98mount (wxPython).

Provides a GUI for viewing, installing, removing, enabling, disabling,
and reloading plugins at runtime.  Opened from the Plugins menu.
"""

import wx

import registry
import plugin_loader
from mount_backend import open_in_file_manager


class PluginManagerDialog(wx.Dialog):
    """Dialog for managing plugins."""

    def __init__(self, parent):
        super().__init__(
            parent, title="Plugin Manager",
            size=(700, 460),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.SetMinSize((560, 360))
        self.plugins_changed = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Plugin list ──────────────────────────────────────────
        self.list_ctrl = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.AppendColumn("Plugin", width=180)
        self.list_ctrl.AppendColumn("Provides", width=260)
        self.list_ctrl.AppendColumn("File", width=220)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED,
                            self._on_selection_change)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED,
                            self._on_selection_change)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # ── Detail label ─────────────────────────────────────────
        self.detail_label = wx.StaticText(self, label="")
        self.detail_label.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self.detail_label, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # ── Button row ───────────────────────────────────────────
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_install = wx.Button(self, label="Install\u2026")
        self.btn_install.SetToolTip(
            "Copy a .py plugin file into the plugins folder")
        self.btn_install.Bind(wx.EVT_BUTTON, self._on_install)
        btn_sizer.Add(self.btn_install, 0, wx.RIGHT, 4)

        self.btn_toggle = wx.Button(self, label="Disable")
        self.btn_toggle.SetToolTip(
            "Enable or disable the selected plugin")
        self.btn_toggle.Bind(wx.EVT_BUTTON, self._on_toggle)
        self.btn_toggle.Disable()
        btn_sizer.Add(self.btn_toggle, 0, wx.RIGHT, 4)

        self.btn_remove = wx.Button(self, label="Remove")
        self.btn_remove.SetToolTip(
            "Unload the selected plugin and delete its file")
        self.btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        self.btn_remove.Disable()
        btn_sizer.Add(self.btn_remove, 0, wx.RIGHT, 4)

        self.btn_reload = wx.Button(self, label="Reload")
        self.btn_reload.SetToolTip(
            "Unload and re-import the selected plugin")
        self.btn_reload.Bind(wx.EVT_BUTTON, self._on_reload)
        self.btn_reload.Disable()
        btn_sizer.Add(self.btn_reload, 0, wx.RIGHT, 16)

        self.btn_reload_all = wx.Button(self, label="Reload All")
        self.btn_reload_all.SetToolTip(
            "Unload every plugin and rescan all directories")
        self.btn_reload_all.Bind(wx.EVT_BUTTON, self._on_reload_all)
        btn_sizer.Add(self.btn_reload_all, 0, wx.RIGHT, 4)

        self.btn_open_dir = wx.Button(self, label="Open Folder")
        self.btn_open_dir.SetToolTip(
            "Open the plugins folder in the file manager")
        self.btn_open_dir.Bind(wx.EVT_BUTTON, self._on_open_dir)
        btn_sizer.Add(self.btn_open_dir, 0)

        btn_sizer.AddStretchSpacer()

        btn_close = wx.Button(self, wx.ID_CLOSE, "Close")
        btn_close.Bind(wx.EVT_BUTTON, self._on_close)
        btn_sizer.Add(btn_close, 0)

        sizer.Add(btn_sizer, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

        self.SetSizer(sizer)

    # ── List population ──────────────────────────────────────────

    @staticmethod
    def _provides_str(info):
        """Build a short summary of what a plugin provides."""
        if not info.enabled:
            return "(disabled)"
        regs = registry.get_registrations_for_source(info.module_name)
        parts = []
        if regs['formats']:
            parts.append(f"formats: {', '.join(regs['formats'])}")
        if regs['detectors']:
            parts.append(f"detectors: {', '.join(regs['detectors'])}")
        if regs['probers']:
            parts.append(f"fs: {', '.join(regs['probers'])}")
        return "; ".join(parts) if parts else "(no registrations)"

    def _refresh_list(self):
        """Rebuild the list control from current plugin state."""
        self.list_ctrl.DeleteAllItems()
        # Core first, then user; alphabetical within each group.
        self._infos = sorted(
            plugin_loader.get_all_plugin_info(),
            key=lambda p: (not p.builtin, p.display_name),
        )
        core_color = wx.Colour(140, 140, 140)
        disabled_color = wx.Colour(170, 170, 170)

        for i, info in enumerate(self._infos):
            name = info.display_name
            if info.builtin:
                name += "  (core)"
            elif not info.enabled:
                name += "  (disabled)"

            idx = self.list_ctrl.InsertItem(i, name)
            self.list_ctrl.SetItem(
                idx, 1, self._provides_str(info))
            self.list_ctrl.SetItem(idx, 2, info.file_path.name)

            if info.builtin:
                self.list_ctrl.SetItemTextColour(idx, core_color)
            elif not info.enabled:
                self.list_ctrl.SetItemTextColour(idx, disabled_color)

        n_enabled = sum(1 for p in self._infos if p.enabled)
        n_total = len(self._infos)
        self.detail_label.SetLabel(
            f"{n_enabled} of {n_total} plugin(s) enabled")
        self._reset_buttons()

    # ── Selection ────────────────────────────────────────────────

    def _selected_info(self):
        idx = self.list_ctrl.GetFirstSelected()
        if idx == -1 or idx >= len(self._infos):
            return None
        return self._infos[idx]

    def _reset_buttons(self):
        self.btn_toggle.Disable()
        self.btn_toggle.SetLabel("Disable")
        self.btn_remove.Disable()
        self.btn_reload.Disable()

    def _on_selection_change(self, event):
        info = self._selected_info()
        if not info:
            self._reset_buttons()
            n_enabled = sum(1 for p in self._infos if p.enabled)
            self.detail_label.SetLabel(
                f"{n_enabled} of {len(self._infos)} plugin(s) enabled")
            return

        # Toggle button: core plugins can't be disabled
        if info.builtin:
            self.btn_toggle.Disable()
            self.btn_remove.Disable()
            self.btn_reload.Disable()
        else:
            self.btn_toggle.Enable()
            self.btn_toggle.SetLabel(
                "Enable" if not info.enabled else "Disable")
            self.btn_remove.Enable()
            self.btn_reload.Enable(info.enabled)

        # Detail label
        regs = registry.get_registrations_for_source(info.module_name)
        parts = []
        if regs['formats']:
            parts.append(
                f"Image formats: {', '.join(regs['formats'])}")
        if regs['detectors']:
            parts.append(
                f"Partition detectors: "
                f"{', '.join(regs['detectors'])}")
        if regs['probers']:
            parts.append(
                f"Filesystem probers: "
                f"{', '.join(regs['probers'])}")

        if not info.enabled:
            status = "(disabled)"
        elif parts:
            status = " | ".join(parts)
        else:
            status = "(no registrations)"

        self.detail_label.SetLabel(
            f"{info.display_name}: {status}  "
            f"\u2014  {info.file_path}")

    # ── Actions ──────────────────────────────────────────────────

    def _on_toggle(self, event):
        info = self._selected_info()
        if not info or info.builtin:
            return
        if info.enabled:
            plugin_loader.disable_plugin(info.module_name)
        else:
            plugin_loader.enable_plugin(info.module_name)
        self.plugins_changed = True
        self._refresh_list()

    def _on_install(self, event):
        dlg = wx.FileDialog(
            self, "Select Plugin File",
            wildcard="Python files (*.py)|*.py",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            info = plugin_loader.install_plugin_file(path)
            if info:
                self.plugins_changed = True
                self._refresh_list()
            else:
                wx.MessageBox(
                    f"Failed to install plugin:\n{path}",
                    "Plugin Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()

    def _on_remove(self, event):
        info = self._selected_info()
        if not info or info.builtin:
            return
        confirm = wx.MessageBox(
            f"Remove plugin \"{info.display_name}\"?\n\n"
            f"This will delete:\n{info.file_path}\n\n"
            f"This action cannot be undone.",
            "Confirm Removal",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if confirm != wx.YES:
            return
        if plugin_loader.remove_plugin(info.module_name):
            self.plugins_changed = True
            self._refresh_list()
        else:
            wx.MessageBox(
                f"Failed to remove plugin:\n{info.display_name}",
                "Plugin Error", wx.OK | wx.ICON_ERROR)

    def _on_reload(self, event):
        info = self._selected_info()
        if not info:
            return
        if plugin_loader.reload_plugin(info.module_name):
            self.plugins_changed = True
            self._refresh_list()
        else:
            wx.MessageBox(
                f"Failed to reload plugin:\n{info.display_name}",
                "Plugin Error", wx.OK | wx.ICON_ERROR)

    def _on_reload_all(self, event):
        plugin_loader.reload_all_plugins()
        self.plugins_changed = True
        self._refresh_list()

    def _on_open_dir(self, event):
        open_in_file_manager(str(plugin_loader.get_plugin_dir()))

    def _on_close(self, event):
        self.EndModal(wx.ID_CLOSE)
