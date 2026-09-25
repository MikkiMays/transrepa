"""Окно CodeShuttle: вкладки «Экспорт» и «Импорт», общий журнал внизу."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import archiver, config as cfgmod
from . import exporter, gitsync, importer, ship
from .util import Log

PAD = 8
TITLE = "CodeShuttle"
EXPORT_COLORS = ("#1f7a3a", "#155c2b")   # фон кнопки, фон при нажатии
IMPORT_COLORS = ("#1f5fa8", "#174a84")
ROW_COLORS = {"ok": "#1a7f37", "todo": "#0550ae", "warn": "#9a6700", "err": "#cf222e", "plain": "#57606a"}
LOG_COLORS = {"err": "#ff7b72", "warn": "#e3b341", "ok": "#56d364", "head": "#79c0ff"}
PROBLEMS_SHOWN = 12


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class MarkerDialog(tk.Toplevel):
    """Создать или поправить файл ветки для папки-репозитория."""

    def __init__(self, app: "App", folder: Path):
        super().__init__(app.root)
        self.app, self.folder = app, folder
        self.marker = folder / gitsync.MARKER_NAME
        self.title(f"Файл ветки — {folder.name}")
        self.transient(app.root)
        self.resizable(False, False)

        spec = gitsync.read_spec(self.marker) if self.marker.exists() else None
        current = gitsync.current_branch(folder)
        remotes = gitsync.list_remotes(folder) or ["origin"]
        default_remote = "origin" if "origin" in remotes else remotes[0]
        self.v_branch = tk.StringVar(value=(spec.branch if spec and spec.branch else current or ""))
        self.v_remote = tk.StringVar(value=spec.remote if spec else default_remote)
        self.v_checkout = tk.StringVar(
            value=spec.checkout if spec and spec.checkout in gitsync.CHECKOUT_MODES else "auto")
        self.v_dirty = tk.BooleanVar(value=bool(spec and spec.allow_dirty))
        self.v_enabled = tk.BooleanVar(value=spec.enabled if spec else True)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Папка:").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text=str(folder), style="Bold.TLabel", wraplength=520).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(body, text="Сейчас на ветке:").grid(row=1, column=0, sticky="w")
        ttk.Label(body, text=current or "(detached HEAD)").grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(body, text="Remote:").grid(row=2, column=0, sticky="w", pady=(12, 2))
        cb_remote = ttk.Combobox(body, textvariable=self.v_remote, values=remotes, width=18)
        cb_remote.grid(row=2, column=1, sticky="w", padx=6, pady=(12, 2))
        cb_remote.bind("<<ComboboxSelected>>", lambda _e: self._load_branches())
        ttk.Label(body, text="Ветка на remote:").grid(row=3, column=0, sticky="w", pady=2)
        self.cb_branch = ttk.Combobox(body, textvariable=self.v_branch, width=48)
        self.cb_branch.grid(row=3, column=1, sticky="ew", padx=6, pady=2)
        ttk.Label(body, text="в списке — ветки по последнему fetch; можно вписать любую",
                  style="Hint.TLabel").grid(row=4, column=1, sticky="w", padx=6)

        modes = ttk.LabelFrame(body, text="Если репозиторий сейчас на другой ветке", padding=6)
        modes.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 6))
        ttk.Radiobutton(modes, text="переключить автоматически — после всех проверок",
                        value="auto", variable=self.v_checkout).pack(anchor="w")
        ttk.Radiobutton(modes, text="не трогать — остановить выгрузку и попросить переключить самому",
                        value="manual", variable=self.v_checkout).pack(anchor="w")
        ttk.Checkbutton(body, text="разрешить незакоммиченные правки, если они не конфликтуют "
                                   "(уедут в выгрузку)",
                        variable=self.v_dirty).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="включено (снять — папка выгружается как есть, git не трогаем)",
                        variable=self.v_enabled).grid(row=7, column=0, columnspan=2, sticky="w")

        btns = ttk.Frame(body)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(btns, text="Сохранить", command=self._save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=6)
        if self.marker.exists():
            ttk.Button(btns, text="Удалить файл ветки", command=self._delete).pack(side="left")

        self._load_branches()
        self._center_over(app.root)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._save())
        self.grab_set()
        self.cb_branch.focus_set()

    def _center_over(self, parent: tk.Tk) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _load_branches(self) -> None:
        self.cb_branch["values"] = gitsync.remote_branches(self.folder, self.v_remote.get().strip())

    def _save(self) -> None:
        try:
            path = gitsync.write_marker(self.folder, self.v_branch.get().strip(),
                                        self.v_remote.get().strip(), self.v_checkout.get(),
                                        bool(self.v_dirty.get()), bool(self.v_enabled.get()))
        except (ValueError, OSError) as exc:
            messagebox.showerror(TITLE, f"Не сохранено: {exc}", parent=self)
            return
        self.app._log_line(f"Файл ветки сохранён: {path}")
        self.destroy()
        self.app._refresh_tree()

    def _delete(self) -> None:
        if not messagebox.askyesno(TITLE, f"Удалить {self.marker}?\n\n"
                                          "Папка будет выгружаться как есть, без git.", parent=self):
            return
        self.marker.unlink(missing_ok=True)
        self.app._log_line(f"Файл ветки удалён: {self.marker}")
        self.destroy()
        self.app._refresh_tree()


class App:
    def __init__(self, root: tk.Tk, config_path: str | None = None):
        self.root = root
        self.config_path = Path(config_path) if config_path else cfgmod.DEFAULT_CONFIG_PATH
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.action_buttons: list[tk.Widget] = []
        self.tree_folders: dict[str, Path] = {}
        self.shown_root = ""

        self.config_path, created = cfgmod.ensure_config(self.config_path)
        try:
            self.cfg = cfgmod.load_config(self.config_path)
        except ValueError as exc:
            messagebox.showerror(TITLE, f"Конфиг повреждён:\n{exc}")
            cfgmod.write_default_config(self.config_path)
            self.cfg = cfgmod.load_config(self.config_path)
            created = True
        self.first_run = created

        root.title(f"CodeShuttle — перенос кода батчами  ({self.config_path.name})")
        root.geometry("1080x780")
        root.minsize(900, 640)

        self.vars = {
            "project_root": tk.StringVar(value=self.cfg.get("project_root", "")),
            "export_dir": tk.StringVar(value=self.cfg.get("export_dir", "")),
            "archive_dir": tk.StringVar(value=self.cfg.get("archive_dir", "")),
            "import_dir": tk.StringVar(value=self.cfg.get("import_dir", "")),
            "max_batch_kb": tk.StringVar(value=f"{float(self.cfg.get('max_batch_kb') or 45):g}"),
            "ship_kind": tk.StringVar(value=self.cfg.get("ship_kind") or "full"),
            "confirm_switch": tk.BooleanVar(value=self.cfg.get("git", {}).get("confirm_switch", True)),
        }
        self._build()
        self.root.after(100, self._drain)
        if self.first_run:
            self._log_line("Первый запуск: создан конфиг со значениями по умолчанию.\n"
                           "Укажи корень проекта и папки на нужной вкладке, затем «Сохранить настройки».")
        else:
            self._log_line("CodeShuttle готов. Экспорт — отправить код отсюда, импорт — принять присланное.")
        self._log_line(f"Конфиг: {self.config_path}")
        self._refresh_tree()

    # ------------------------------------------------------------ вёрстка
    def _styles(self) -> None:
        style = ttk.Style()
        style.configure("Hint.TLabel", foreground="#6e7781")
        style.configure("Bold.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(16, 6), font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=22)

    def _build(self) -> None:
        self._styles()
        top = ttk.Frame(self.root, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Корень проекта:", style="Bold.TLabel").grid(row=0, column=0, sticky="w")
        root_entry = ttk.Entry(top, textvariable=self.vars["project_root"])
        root_entry.grid(row=0, column=1, sticky="ew", padx=6)
        root_entry.bind("<Return>", lambda _e: self._refresh_tree())
        root_entry.bind("<FocusOut>", lambda _e: self._refresh_tree(only_if_changed=True))
        ttk.Button(top, text="Обзор…", width=9, command=lambda: self._pick("project_root")).grid(row=0, column=2)
        ttk.Button(top, text="Открыть", width=9, command=lambda: self._open("project_root")).grid(
            row=0, column=3, padx=(4, 0))
        ttk.Label(top, text="Экспорт выгружает эту папку целиком (со всеми вложенными), "
                            "импорт раскладывает файлы в неё.",
                  style="Hint.TLabel").grid(row=1, column=1, sticky="w", padx=6)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="x", padx=PAD, pady=(6, 4))
        exp = ttk.Frame(self.nb, padding=PAD)
        imp = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(exp, text="  ⬆  ЭКСПОРТ — отправить отсюда  ")
        self.nb.add(imp, text="  ⬇  ИМПОРТ — принять сюда  ")
        self._build_export(exp)
        self._build_import(imp)
        if self.cfg.get("gui_tab") == "import":
            self.nb.select(1)

        logframe = ttk.LabelFrame(self.root, text="Журнал", padding=4)
        logframe.pack(fill="both", expand=True, padx=PAD, pady=(4, 4))
        self.text = tk.Text(logframe, wrap="none", height=10, bg="#101418", fg="#d6dee6",
                            insertbackground="#d6dee6", font=("Consolas", 10), relief="flat")
        for tag, color in LOG_COLORS.items():
            self.text.tag_configure(tag, foreground=color)
        yscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(logframe, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        logframe.rowconfigure(0, weight=1)
        logframe.columnconfigure(0, weight=1)

        bottom = ttk.Frame(self.root, padding=(PAD, 0, PAD, PAD))
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="готов")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=10)
        ttk.Button(bottom, text="Очистить журнал", command=self._clear).pack(side="right")
        ttk.Button(bottom, text="Сохранить настройки", command=self._save_cfg).pack(side="right", padx=6)

    def _path_row(self, parent: ttk.Frame, row: int, key: str, label: str, hint: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(parent, text="Обзор…", width=9, command=lambda: self._pick(key)).grid(row=row, column=2)
        ttk.Button(parent, text="Открыть", width=9, command=lambda: self._open(key)).grid(
            row=row, column=3, padx=(4, 0))
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row, column=4, sticky="w", padx=(8, 0))

    def _big_button(self, parent: tk.Widget, text: str, colors: tuple[str, str], command) -> tk.Button:
        btn = tk.Button(parent, text=text, command=command, bg=colors[0], fg="white",
                        activebackground=colors[1], activeforeground="white",
                        disabledforeground="#c9d1d9", font=("Segoe UI", 12, "bold"),
                        relief="flat", bd=0, cursor="hand2", padx=14, pady=9)
        self.action_buttons.append(btn)
        return btn

    def _button(self, parent: tk.Widget, text: str, command, action: bool = True) -> ttk.Button:
        btn = ttk.Button(parent, text=text, command=command)
        if action:
            self.action_buttons.append(btn)
        return btn

    def _build_export(self, parent: ttk.Frame) -> None:
        paths = ttk.Frame(parent)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "export_dir", "Папка батчей (outbox):", "сюда пишутся .txt")
        self._path_row(paths, 1, "archive_dir", "Папка архивов .rar:", "пусто — рядом с проектом")
        ttk.Label(paths, text="Размер батча, КБ:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(paths, textvariable=self.vars["max_batch_kb"], width=8).grid(row=2, column=1, sticky="w", padx=6)

        main = ttk.Frame(parent)
        main.pack(fill="x", pady=(PAD, 0))
        self.btn_ship = self._big_button(main, "⬆   ЭКСПОРТ ОДНОЙ КНОПКОЙ:   git fetch  →  батчи  →  .rar",
                                         EXPORT_COLORS, lambda: self._run(self._ship))
        self.btn_ship.pack(fill="x")
        opts = ttk.Frame(main)
        opts.pack(fill="x", pady=(4, 0))
        ttk.Label(opts, text="Что выгружать:").pack(side="left")
        ttk.Radiobutton(opts, text="весь проект", value="full",
                        variable=self.vars["ship_kind"]).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(opts, text="только изменения с прошлой выгрузки", value="delta",
                        variable=self.vars["ship_kind"]).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="спрашивать перед переключением веток",
                        variable=self.vars["confirm_switch"]).pack(side="right")

        gitf = ttk.LabelFrame(parent, padding=6, text=(
            f"Папки проекта. С файлом {gitsync.MARKER_NAME}: fetch → проверки → переключение на ветку "
            f"из файла. Без него — как есть. Любая проблема останавливает всё до изменений."))
        gitf.pack(fill="x", pady=(PAD, 0))
        cols = {"folder": ("Папка", 200), "branch": ("Ветка из файла", 210),
                "current": ("Сейчас", 150), "state": ("Что будет", 420)}
        treebox = ttk.Frame(gitf)
        treebox.pack(fill="x")
        self.tree = ttk.Treeview(treebox, columns=list(cols), show="headings", height=6, selectmode="browse")
        for key, (title, width) in cols.items():
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=(key == "state"))
        for tag, color in ROW_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        tscroll = ttk.Scrollbar(treebox, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.pack(side="left", fill="x", expand=True)
        tscroll.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._on_tree_double)

        gbtn = ttk.Frame(gitf)
        gbtn.pack(fill="x", pady=(6, 0))
        self._button(gbtn, "Проверить ветки (fetch, ничего не меняя)",
                     lambda: self._run(self._git_check)).pack(side="left")
        self._button(gbtn, "Файл ветки для папки…", self._marker_pick).pack(side="left", padx=6)
        self._button(gbtn, "Обновить список", self._refresh_tree, action=False).pack(side="left")
        ttk.Label(gbtn, text="двойной щелчок по строке — настроить ветку этой папки",
                  style="Hint.TLabel").pack(side="right")

        manual = ttk.LabelFrame(parent, text="Вручную, без git — все папки выгружаются как есть", padding=6)
        manual.pack(fill="x", pady=(PAD, 0))
        for c in range(4):
            manual.columnconfigure(c, weight=1, uniform="manual")
        row1 = [("Весь проект → батчи", lambda: self._run(self._export_full)),
                ("Только изменения → батчи", lambda: self._run(self._export_delta)),
                ("Изменения → батчи → .rar", lambda: self._run(self._archive)),
                ("Что изменилось?", lambda: self._run(self._status))]
        row2 = [("Отметить текущее как синхронизированное", lambda: self._run(self._baseline)),
                ("Открыть outbox", lambda: self._open("export_dir")),
                ("Открыть папку архивов", self._open_archives)]
        for c, (text, cmd) in enumerate(row1):
            self._button(manual, text, cmd).grid(row=0, column=c, sticky="ew", padx=2, pady=2)
        for c, (text, cmd) in enumerate(row2):
            self._button(manual, text, cmd, action=c == 0).grid(row=1, column=c, sticky="ew", padx=2, pady=2)

    def _build_import(self, parent: ttk.Frame) -> None:
        paths = ttk.Frame(parent)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "import_dir", "Папка с батчами (inbox):", "сюда кладёшь присланные .txt")

        self.btn_import = self._big_button(parent, "⬇   ИМПОРТ:   применить батчи из inbox к проекту",
                                           IMPORT_COLORS, lambda: self._run(self._import))
        self.btn_import.pack(fill="x", pady=(PAD, 4))
        row = ttk.Frame(parent)
        row.pack(fill="x")
        for c in range(3):
            row.columnconfigure(c, weight=1, uniform="imp")
        self._button(row, "Распаковать архив .rar в inbox и применить…", self._import_rar).grid(
            row=0, column=0, sticky="ew", padx=2)
        self._button(row, "Выбрать другую папку с батчами и применить…", self._import_pick).grid(
            row=0, column=1, sticky="ew", padx=2)
        self._button(row, "Проверить без записи", lambda: self._run(self._import_dry)).grid(
            row=0, column=2, sticky="ew", padx=2)

        steps = ttk.LabelFrame(parent, text="Как принять код", padding=(10, 6))
        steps.pack(fill="x", pady=(PAD, 0))
        for n, text in enumerate((
                "Прислали архив .rar — «Распаковать архив .rar в inbox и применить…»: архив "
                "распакуется в inbox/<имя архива>/ и сразу применится.",
                "Прислали .txt-батчи — положи их в inbox (в любом порядке, можно с дубликатами) "
                "и нажми синюю кнопку.",
                "Не уверен, что всё доехало, — «Проверить без записи»: покажет, что будет создано, "
                "обновлено и удалено, и каких батчей не хватает.",
                "Экспорт на отправителе был «только изменения»? Сначала должен быть применён "
                "полный пак, иначе в проекте окажутся только изменённые файлы."), start=1):
            ttk.Label(steps, text=f"{n}.", style="Bold.TLabel").grid(row=n, column=0, sticky="nw", pady=1)
            ttk.Label(steps, text=text, wraplength=960, justify="left").grid(
                row=n, column=1, sticky="w", padx=(4, 0), pady=1)

        ttk.Label(parent, style="Hint.TLabel", justify="left", wraplength=980, text=(
            "Батчи можно сваливать в inbox как угодно: в любом порядке, с дубликатами, полные и "
            "изменения вперемешку — каждый пак применится один раз и по порядку. Файл с несошедшейся "
            "контрольной суммой не пишется. Импорт пишет прямо в проект, без резервных копий; "
            "применённые батчи уезжают в inbox/_applied/.")).pack(fill="x", pady=(PAD, 0))

    # ------------------------------------------------------------ таблица папок
    def _row_for_spec(self, found: gitsync.Discovery, spec: gitsync.BranchSpec,
                      repo: gitsync.RepoPlan | None, current: str = "", applied: bool = False):
        name = found.rel(spec.folder)
        if spec.errors:
            return spec.folder, (name, spec.summary, current, "✖ " + spec.errors[0]), "err"
        if not spec.enabled:
            return spec.folder, (name, spec.summary, current, "— файл выключен, выгрузится как есть"), "plain"
        if repo is None:
            return spec.folder, (name, spec.summary, current, "? не проверено — «Проверить ветки»"), "todo"
        if applied and repo.ok and repo.target:
            state = f"✔ на {spec.branch} @ {repo.target[:9]} — выгружено"
            return spec.folder, (name, spec.summary, spec.branch, state), "ok"
        tag = "err" if repo.errors else "warn" if repo.warnings else "ok" if repo.action == "none" else "todo"
        return spec.folder, (name, spec.summary, repo.current_label if repo.toplevel else "", repo.describe()), tag

    def _plain_rows(self, found: gitsync.Discovery) -> list:
        return [(p, (found.rel(p), "—", "", "— без файла ветки, выгрузится как есть"), "plain")
                for p in found.plain]

    def _rows_from_discovery(self, found: gitsync.Discovery) -> list:
        """Без fetch: только что лежит на диске. Вызывается из рабочего потока."""
        has_git = gitsync.git_available()
        rows = []
        for s in found.specs:
            current = (gitsync.current_branch(s.folder) or "(detached)") if has_git and not s.errors else ""
            rows.append(self._row_for_spec(found, s, None, current))
        return rows + self._plain_rows(found)

    def _rows_from_sync(self, sync: gitsync.SyncPlan, applied: bool = False) -> list:
        checked = {id(r.spec): r for r in sync.repos}
        found = sync.discovery
        rows = [self._row_for_spec(found, s, checked.get(id(s)), applied=applied) for s in found.specs]
        return rows + self._plain_rows(found)

    def _fill_tree(self, rows: list, placeholder: str = "") -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree_folders.clear()
        for folder, values, tag in rows:
            iid = self.tree.insert("", "end", values=values, tags=(tag,))
            self.tree_folders[iid] = folder
        if not rows:
            self.tree.insert("", "end", values=("", "", "", placeholder or "папок нет"), tags=("plain",))

    def _refresh_tree(self, only_if_changed: bool = False) -> None:
        self._sync_cfg()
        root = self.cfg.get("project_root", "")
        if only_if_changed and root == self.shown_root:
            return
        self.shown_root = root
        if not root or not Path(root).is_dir():
            self._fill_tree([], "укажи существующий корень проекта")
            return
        cfg = dict(self.cfg)

        def work() -> None:
            try:
                rows = self._rows_from_discovery(gitsync.discover(cfg))
            except Exception as exc:  # таблица — подсказка, окно из-за неё падать не должно
                self._post(lambda: self._fill_tree([], f"не удалось обойти папку: {exc}"))
                return
            self._post(lambda: self._fill_tree(rows))

        threading.Thread(target=work, daemon=True).start()

    def _live_rows(self, sync: gitsync.SyncPlan) -> None:
        rows = self._rows_from_sync(sync)
        self._post(lambda: self._fill_tree(rows))

    def _on_tree_double(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        folder = self.tree_folders.get(iid)
        if folder is not None:
            self._edit_marker(folder)

    def _marker_pick(self) -> None:
        initial = self.vars["project_root"].get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Папка-репозиторий, для которой задать ветку",
                                         initialdir=initial)
        if chosen:
            self._edit_marker(Path(chosen))

    def _edit_marker(self, folder: Path) -> None:
        if self.busy:
            messagebox.showinfo(TITLE, "Дождись окончания операции")
            return
        root = Path(self.vars["project_root"].get().strip() or ".").resolve()
        folder = folder.resolve()
        if folder != root and root not in folder.parents:
            messagebox.showwarning(TITLE, f"Папка вне корня проекта:\n{folder}\n\n"
                                          "Файл ветки там не будет найден при выгрузке.")
            return
        if not gitsync.git_available():
            messagebox.showerror(TITLE, "git не найден в PATH.")
            return
        if gitsync.repo_toplevel(folder) is None:
            messagebox.showinfo(TITLE, f"{folder.name} — не git-репозиторий.\n\n"
                                       "Такая папка выгружается как есть; файл ветки ей не нужен.")
            return
        MarkerDialog(self, folder)

    # ------------------------------------------------------------ действия
    def _pick(self, key: str) -> None:
        initial = self.vars[key].get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Выбери папку", initialdir=initial)
        if chosen:
            self.vars[key].set(str(Path(chosen)))
            if key == "project_root":
                self._refresh_tree()

    def _open(self, key: str) -> None:
        path = self.vars[key].get().strip()
        if not path:
            messagebox.showinfo(TITLE, "Путь не задан")
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        open_path(Path(path))

    def _open_archives(self) -> None:
        self._sync_cfg()
        if not self.cfg.get("project_root") and not self.cfg.get("archive_dir"):
            messagebox.showinfo(TITLE, "Не задан ни корень проекта, ни папка архивов")
            return
        folder = archiver.default_archive_dir(self.cfg)
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def _sync_cfg(self) -> None:
        for key in ("project_root", "export_dir", "archive_dir", "import_dir"):
            self.cfg[key] = self.vars[key].get().strip()
        try:
            kb = float(self.vars["max_batch_kb"].get().replace(",", "."))
            self.cfg["max_batch_kb"] = int(kb) if kb.is_integer() else kb
        except ValueError:
            self.cfg["max_batch_kb"] = 45
        self.cfg["ship_kind"] = self.vars["ship_kind"].get()
        self.cfg.setdefault("git", {})["confirm_switch"] = bool(self.vars["confirm_switch"].get())
        self.cfg["gui_tab"] = "import" if self.nb.index("current") == 1 else "export"

    def _save_cfg(self) -> None:
        self._sync_cfg()
        path = cfgmod.save_config(self.cfg, self.config_path)
        self._log_line(f"Настройки сохранены в {path}")
        self._refresh_tree(only_if_changed=True)

    def _clear(self) -> None:
        self.text.delete("1.0", "end")

    def _export_full(self, log: Log) -> None:
        exporter.export(self.cfg, "full", log)

    def _export_delta(self, log: Log) -> None:
        exporter.export(self.cfg, "delta", log)

    def _status(self, log: Log) -> None:
        exporter.status(self.cfg, log)

    def _baseline(self, log: Log) -> None:
        exporter.reset_state(self.cfg, log)

    def _archive(self, log: Log) -> None:
        archiver.archive_module(self.cfg, log=log)

    def _git_check(self, log: Log) -> None:
        errors = cfgmod.validate(self.cfg)
        if errors:
            for e in errors:
                log(f"[ошибка] {e}")
            return
        sync = gitsync.preflight(self.cfg, log, on_repo=self._live_rows)
        self._live_rows(sync)

    def _ship(self, log: Log) -> None:
        res = ship.ship(self.cfg, log, confirm=self._confirm_switch, on_repo=self._live_rows)
        if res.sync is not None:
            rows = self._rows_from_sync(res.sync, applied=res.stage == "done")
            self._post(lambda: self._fill_tree(rows))
        self._post(lambda: self._ship_done(res))

    def _confirm_switch(self, sync: gitsync.SyncPlan) -> bool:
        return self._ask("Переключить ветки?", gitsync.switch_question(sync))

    def _ship_done(self, res: ship.ShipResult) -> None:
        if res.stage == "done":
            ar = res.archive
            if ar is not None and ar.rar_path is not None:
                if messagebox.askyesno(TITLE, f"Готово.\n\nАрхив: {ar.rar_path}\n"
                                              f"Батчей внутри: {len(ar.batch_files)}\n\n"
                                              "Открыть папку с архивом?"):
                    open_path(ar.rar_path.parent)
            else:
                messagebox.showinfo(TITLE, f"{res.message or 'Изменений нет'} — архив не создавался.")
        elif res.stage == "git":
            problems = res.sync.problems() if res.sync else []
            text = "\n".join(f"• {p}" for p in problems[:PROBLEMS_SHOWN])
            if len(problems) > PROBLEMS_SHOWN:
                text += f"\n… и ещё {len(problems) - PROBLEMS_SHOWN}"
            messagebox.showerror("Выгрузка остановлена",
                                 "Ни одна ветка не переключена, батчи не создавались.\n\n"
                                 f"{text}\n\nПодробности — в журнале.")
        elif res.stage == "cancel":
            messagebox.showinfo(TITLE, "Отменено: ветки не переключались, выгрузка не выполнялась.\n\n"
                                       "Переключи ветки сам и нажми кнопку снова.")
        elif res.stage == "config":
            messagebox.showerror(TITLE, f"Проверь настройки:\n{res.message}")
        else:
            messagebox.showerror(TITLE, "Не получилось — подробности в журнале.")

    def _import_pick(self) -> None:
        """Спросить папку с батчами и сразу применить её."""
        if self.busy:
            messagebox.showinfo(TITLE, "Операция уже выполняется")
            return
        initial = self.vars["import_dir"].get() or str(Path.home())
        folder = filedialog.askdirectory(title="Откуда грузить батчи?", initialdir=initial)
        if not folder:
            return
        self.vars["import_dir"].set(str(Path(folder)))
        self._run(self._import)

    def _import_rar(self) -> None:
        """Распаковать присланный .rar в inbox и применить."""
        if self.busy:
            messagebox.showinfo(TITLE, "Операция уже выполняется")
            return
        if not self.vars["import_dir"].get().strip():
            messagebox.showinfo(TITLE, "Сначала укажи папку inbox — архив распакуется туда.")
            return
        rar = filedialog.askopenfilename(title="Архив с батчами", initialdir=str(Path.home() / "Downloads"),
                                         filetypes=[("Архив RAR", "*.rar"), ("Все файлы", "*.*")])
        if not rar:
            return

        def work(log: Log) -> None:
            inbox = Path(self.cfg["import_dir"])
            if archiver.extract_rar(Path(rar), inbox / Path(rar).stem, log):
                importer.apply_import(self.cfg, log)

        self._run(work)

    def _import(self, log: Log) -> None:
        importer.apply_import(self.cfg, log)

    def _import_dry(self, log: Log) -> None:
        importer.apply_import(self.cfg, log, dry_run=True)

    # ------------------------------------------------------------ поток/лог
    def _post(self, fn) -> None:
        """Выполнить fn в потоке окна (из рабочего потока tkinter трогать нельзя)."""
        self.queue.put(fn)

    def _ask(self, title: str, question: str) -> bool:
        """Вопрос да/нет из рабочего потока: окно показывает, поток ждёт ответа."""
        answered, box = threading.Event(), {}

        def show() -> None:
            box["yes"] = messagebox.askyesno(title, question, parent=self.root)
            answered.set()

        self._post(show)
        answered.wait()
        return box.get("yes", False)

    def _run(self, fn) -> None:
        if self.busy:
            messagebox.showinfo(TITLE, "Операция уже выполняется")
            return
        self._sync_cfg()
        cfgmod.save_config(self.cfg, self.config_path)
        self.busy = True
        self._buttons(False)
        self.status.configure(text="работаю…")
        self.progress.start(12)
        log = Log(self.queue.put)

        def worker() -> None:
            try:
                fn(log)
            except Exception:
                log("\n[СБОЙ]\n" + traceback.format_exc())
            finally:
                self.queue.put("\x00done")

        threading.Thread(target=worker, daemon=True).start()

    def _buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in self.action_buttons:
            b.configure(state=state)

    def _log_line(self, msg: str) -> None:
        tag = ""
        if "✖" in msg or "[ошибка]" in msg or "[СБОЙ]" in msg:
            tag = "err"
        elif "⚠" in msg or "[внимание]" in msg:
            tag = "warn"
        elif "✔" in msg or msg.lstrip().startswith(("Готово", "Архив:", "Распаковано:")):
            tag = "ok"
        elif msg.lstrip().startswith("==="):
            tag = "head"
        self.text.insert("end", msg + "\n", tag)
        self.text.see("end")

    def _drain(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                if callable(msg):
                    msg()
                elif msg == "\x00done":
                    self.busy = False
                    self._buttons(True)
                    self.progress.stop()
                    self.status.configure(text="готов")
                else:
                    self._log_line(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)


def run_gui(config_path: str | None = None) -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, config_path)
    root.mainloop()
