"""Окно с кнопками: экспорт / импорт в один клик."""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import archiver, config as cfgmod
from . import exporter, importer
from .util import Log

PAD = 8


class App:
    def __init__(self, root: tk.Tk, config_path: str | None = None):
        self.root = root
        self.config_path = Path(config_path) if config_path else cfgmod.DEFAULT_CONFIG_PATH
        self.queue: queue.Queue[str] = queue.Queue()
        self.busy = False

        self.config_path, created = cfgmod.ensure_config(self.config_path)
        try:
            self.cfg = cfgmod.load_config(self.config_path)
        except ValueError as exc:
            messagebox.showerror("CodeShuttle", f"Конфиг повреждён:\n{exc}")
            cfgmod.write_default_config(self.config_path)
            self.cfg = cfgmod.load_config(self.config_path)
            created = True
        self.first_run = created

        root.title(f"CodeShuttle — перенос кода батчами  ({self.config_path.name})")
        root.geometry("980x680")
        root.minsize(760, 520)

        self.vars = {
            "project_root": tk.StringVar(value=self.cfg.get("project_root", "")),
            "export_dir": tk.StringVar(value=self.cfg.get("export_dir", "")),
            "import_dir": tk.StringVar(value=self.cfg.get("import_dir", "")),
            "max_batch_kb": tk.StringVar(value=str(self.cfg.get("max_batch_kb", 45))),
        }
        self._build()
        self.root.after(100, self._drain)
        if self.first_run:
            self._log_line("Первый запуск: создан конфиг со значениями по умолчанию.\n"
                           "Заполни три пути сверху и нажми «Сохранить настройки».")
        else:
            self._log_line("CodeShuttle готов. Проверь пути и жми кнопку.")
        self._log_line(f"Конфиг: {self.config_path}")

    # ------------------------------------------------------------ вёрстка
    def _build(self) -> None:
        paths = ttk.LabelFrame(self.root, text="Пути", padding=PAD)
        paths.pack(fill="x", padx=PAD, pady=(PAD, 4))
        paths.columnconfigure(1, weight=1)

        rows = [
            ("project_root", "Корень проекта", "Откуда выгружаем и куда раскладываем импорт"),
            ("export_dir", "Папка выгрузки (outbox)", "Сюда пишутся батчи .txt"),
            ("import_dir", "Папка загрузки (inbox)", "Отсюда читаются батчи для применения"),
        ]
        for r, (key, label, hint) in enumerate(rows):
            ttk.Label(paths, text=label + ":").grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(paths, textvariable=self.vars[key]).grid(row=r, column=1, sticky="ew", padx=6)
            ttk.Button(paths, text="Обзор…", width=9,
                       command=lambda k=key: self._pick(k)).grid(row=r, column=2)
            ttk.Label(paths, text=hint, foreground="#777").grid(row=r, column=3, sticky="w", padx=(6, 0))

        ttk.Label(paths, text="Размер батча, КБ:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(paths, textvariable=self.vars["max_batch_kb"], width=8).grid(
            row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Button(paths, text="Сохранить настройки", command=self._save_cfg).grid(
            row=3, column=2, columnspan=2, sticky="w", padx=(6, 0), pady=(6, 0))

        actions = ttk.Frame(self.root, padding=(PAD, 4))
        actions.pack(fill="x")

        exp = ttk.LabelFrame(actions, text="Отправить (эта машина)", padding=PAD)
        exp.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_full = ttk.Button(exp, text="▶  ВЫГРУЗИТЬ ВЕСЬ ПРОЕКТ",
                                   command=lambda: self._run(self._export_full))
        self.btn_full.pack(fill="x", pady=2)
        self.btn_delta = ttk.Button(exp, text="▶  Выгрузить только изменения",
                                    command=lambda: self._run(self._export_delta))
        self.btn_delta.pack(fill="x", pady=2)
        row = ttk.Frame(exp)
        row.pack(fill="x", pady=2)
        ttk.Button(row, text="Что изменилось?", command=lambda: self._run(self._status)).pack(
            side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(row, text="Открыть outbox", command=lambda: self._open("export_dir")).pack(
            side="left", expand=True, fill="x", padx=(2, 0))
        ttk.Button(exp, text="Отметить текущее состояние как синхронизированное",
                   command=lambda: self._run(self._baseline)).pack(fill="x", pady=2)
        self.btn_archive = ttk.Button(exp, text="Выгрузить изменения и упаковать батчи в .rar",
                                      command=lambda: self._run(self._archive))
        self.btn_archive.pack(fill="x", pady=2)

        imp = ttk.LabelFrame(actions, text="Принять (другая машина)", padding=PAD)
        imp.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.btn_import = ttk.Button(imp, text="◀  ПРИМЕНИТЬ БАТЧИ ИЗ INBOX",
                                     command=lambda: self._run(self._import))
        self.btn_import.pack(fill="x", pady=2)
        self.btn_pick = ttk.Button(imp, text="◀  Выбрать папку с батчами и применить…",
                                   command=self._import_pick)
        self.btn_pick.pack(fill="x", pady=2)
        row2 = ttk.Frame(imp)
        row2.pack(fill="x", pady=2)
        self.btn_check = ttk.Button(row2, text="Проверить (без записи)",
                                    command=lambda: self._run(self._import_dry))
        self.btn_check.pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(row2, text="Открыть inbox", command=lambda: self._open("import_dir")).pack(
            side="left", expand=True, fill="x", padx=(2, 0))

        logframe = ttk.LabelFrame(self.root, text="Журнал", padding=4)
        logframe.pack(fill="both", expand=True, padx=PAD, pady=(4, PAD))
        self.text = tk.Text(logframe, wrap="none", height=20, bg="#101418", fg="#d6dee6",
                            insertbackground="#d6dee6", font=("Consolas", 10), relief="flat")
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
        ttk.Button(bottom, text="Очистить журнал", command=self._clear).pack(side="right")

    # ------------------------------------------------------------ действия
    def _pick(self, key: str) -> None:
        initial = self.vars[key].get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Выбери папку", initialdir=initial)
        if chosen:
            self.vars[key].set(str(Path(chosen)))

    def _open(self, key: str) -> None:
        path = self.vars[key].get().strip()
        if not path:
            messagebox.showinfo("CodeShuttle", "Путь не задан")
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", path])

    def _sync_cfg(self) -> None:
        for key in ("project_root", "export_dir", "import_dir"):
            self.cfg[key] = self.vars[key].get().strip()
        try:
            self.cfg["max_batch_kb"] = float(self.vars["max_batch_kb"].get().replace(",", "."))
        except ValueError:
            self.cfg["max_batch_kb"] = 45

    def _save_cfg(self) -> None:
        self._sync_cfg()
        path = cfgmod.save_config(self.cfg, self.config_path)
        self._log_line(f"Настройки сохранены в {path}")

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

    def _import_pick(self) -> None:
        """Спросить папку с батчами и сразу применить её."""
        if self.busy:
            messagebox.showinfo("CodeShuttle", "Операция уже выполняется")
            return
        initial = self.vars["import_dir"].get() or str(Path.home())
        folder = filedialog.askdirectory(title="Откуда грузить батчи?", initialdir=initial)
        if not folder:
            return
        self.vars["import_dir"].set(str(Path(folder)))
        self._run(self._import)

    def _import(self, log: Log) -> None:
        importer.apply_import(self.cfg, log)

    def _import_dry(self, log: Log) -> None:
        importer.apply_import(self.cfg, log, dry_run=True)

    # ------------------------------------------------------------ поток/лог
    def _run(self, fn) -> None:
        if self.busy:
            messagebox.showinfo("CodeShuttle", "Операция уже выполняется")
            return
        self._sync_cfg()
        cfgmod.save_config(self.cfg, self.config_path)
        self.busy = True
        self._buttons(False)
        self.status.configure(text="работаю…")
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
        for b in (self.btn_full, self.btn_delta, self.btn_archive, self.btn_import, self.btn_check, self.btn_pick):
            b.configure(state=state)

    def _log_line(self, msg: str) -> None:
        self.text.insert("end", msg + "\n")
        self.text.see("end")

    def _drain(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg == "\x00done":
                    self.busy = False
                    self._buttons(True)
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
