"""Two-tab WR503 status-history and vibration-analysis desktop app."""
from __future__ import annotations

import json, os, queue, sqlite3, sys, threading, time, tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Microsoft JhengHei", "Microsoft YaHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector

from app.analysis import (ProcessingSettings, analyze_recording, diagnose_range, frequency_analysis,
                          infer_source_hz, iso10816_reference, metrics_for_range, status_mask_intervals,
                          stft_analysis)
from app.data import infer_sensor_start, load_sensor_csv
from app.network import matching_adapter
from app.protocol import RASeMIOClient
from app.storage import ObservationStore

APP_NAME = "WR503 Status & Vibration"
CONNECTION_TIMEOUT_S = 3.0
POLLING_INTERVAL_S = 0.2
RETRY_INTERVAL_S = 10.0
MAX_CONSECUTIVE_FAILURES = 5
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def fallback_data_dir() -> Path:
    """使用者的本機應用程式資料夾，永遠位於本機磁碟。不在此處建立資料夾，用到才建。"""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WR503-GPST-Monitor"


def open_store(preferred: Path) -> tuple[ObservationStore, Path]:
    """優先在執行檔旁邊建資料庫。唯讀目錄、共享資料夾或網路磁碟會失敗，改用本機應用程式資料夾。

    共享資料夾的寫入測試會通過，但 SQLite 仍可能回報 disk I/O error，所以判斷依據是實際開啟資料庫，
    不是寫入測試。
    """
    candidates = [preferred / "wr503_status.sqlite3"]
    fallback = fallback_data_dir() / "wr503_status.sqlite3"
    if fallback != candidates[0]:
        candidates.append(fallback)
    last_error: Exception | None = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return ObservationStore(path), path.parent
        except (sqlite3.Error, OSError) as error:
            last_error = error
    raise last_error if last_error else RuntimeError("no database path available")


BUNDLED_CONFIG_PATH = APP_DIR / "wr503_app_config.json"
DATA_DIR = APP_DIR
DB_PATH = APP_DIR / "wr503_status.sqlite3"
CONFIG_PATH = APP_DIR / "wr503_app_config.json"


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Acquisition(threading.Thread):
    COMMANDS = ("GPST", "GMST", "GSID", "GSPD", "GACC", "GDEC", "GJNT", "GENC", "GPOS")
    def __init__(self, events, stop, settings, database_path):
        super().__init__(daemon=True)
        self.events = events
        self.stop = stop
        self.settings = settings
        self.database_path = database_path
    def run(self):
        interval = POLLING_INTERVAL_S
        pending = []
        last_flush = time.monotonic()
        consecutive_failures = 0
        terminal_error = None
        store = ObservationStore(self.database_path)
        while not self.stop.is_set():
            client = RASeMIOClient(str(self.settings["host"]), int(self.settings["port"]), CONNECTION_TIMEOUT_S, str(self.settings["local_ip"]))
            try:
                client.connect(); self.events.put(("connected", None))
                while not self.stop.is_set():
                    began = time.perf_counter()
                    responses = {command: client.query(command) for command in self.COMMANDS}
                    consecutive_failures = 0
                    speed = responses["GSPD"].data
                    observation = {
                        "timestamp": utc_text(),
                        "mode": responses["GPST"].mode,
                        "motion_status": responses["GMST"].mode,
                        "selected_gripper": responses["GSID"].mode,
                        "ptp_speed_pct": speed[0],
                        "linear_speed_mm_s": speed[1],
                        "speed_ratio_pct": speed[2],
                        "acceleration_ms": responses["GACC"].data[0],
                        "deceleration_ms": responses["GDEC"].data[0],
                        "commanded_joints": store.encode_values(responses["GJNT"].data[:responses["GJNT"].mode]),
                        "encoder_joints": store.encode_values(responses["GENC"].data[:responses["GENC"].mode]),
                        "commanded_position": store.encode_values(responses["GPOS"].data[:responses["GPOS"].mode]),
                    }
                    pending.append(observation)
                    if len(pending) >= 10 or time.monotonic() - last_flush >= .5:
                        store.append_many(pending); pending.clear(); last_flush = time.monotonic()
                    self.events.put(("sample", observation))
                    self.stop.wait(max(.01, interval - time.perf_counter() + began))
            except Exception as exc:
                if not self.stop.is_set():
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        terminal_error = f"連續 {MAX_CONSECUTIVE_FAILURES} 次連線失敗，已停止：{exc}"
                        break
                    self.events.put(("retry", f"第 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES} 次失敗；10 秒後重試：{exc}"))
                    self.stop.wait(RETRY_INTERVAL_S)
            finally: client.close()
        if pending: store.append_many(pending)
        store.close(); self.events.put(("stopped", terminal_error))


class App(tk.Tk):
    HISTORY_FIELDS = {
        "取片狀態": ("mode", "GPST"), "動作狀態": ("motion_status", "GMST"),
        "選定牙叉": ("selected_gripper", "GSID"), "點到點速度 (%)": ("ptp_speed_pct", "GSPD[0]"),
        "直線速度 (mm/s)": ("linear_speed_mm_s", "GSPD[1]"), "速度比例 (%)": ("speed_ratio_pct", "GSPD[2]"),
        "加速時間 (ms)": ("acceleration_ms", "GACC"), "減速時間 (ms)": ("deceleration_ms", "GDEC"),
        "命令關節角度 (deg)": ("commanded_joints", "GJNT"), "編碼器關節角度 (deg)": ("encoder_joints", "GENC"),
        "命令位置 (mm)": ("commanded_position", "GPOS"),
    }
    STATUS_FIELDS = (
        ("取片 GPST", "mode"), ("動作 GMST", "motion_status"),
        ("牙叉 GSID", "selected_gripper"), ("點到點速度", "ptp_speed_pct"),
        ("直線速度", "linear_speed_mm_s"), ("速度比例", "speed_ratio_pct"),
        ("加速時間", "acceleration_ms"), ("減速時間", "deceleration_ms"),
        ("命令關節", "commanded_joints"), ("實際關節", "encoder_joints"),
        ("命令位置", "commanded_position"),
    )
    CATEGORICAL_STATUS = {
        "mode": ({0:"無片",1:"上手有片",2:"下手有片",3:"雙手有片"},
                 {0:"#64748b",1:"#2563eb",2:"#0891b2",3:"#7c3aed"}),
        "motion_status": ({0:"動作中",1:"停止",2:"暫停",3:"延遲"},
                          {0:"#2563eb",1:"#16a34a",2:"#d97706",3:"#7c3aed"}),
        "selected_gripper": ({}, {0:"#0891b2",1:"#2563eb",2:"#7c3aed",3:"#0f766e"}),
    }
    SETTING_STATUS = frozenset({"ptp_speed_pct","linear_speed_mm_s","speed_ratio_pct","acceleration_ms","deceleration_ms"})
    def __init__(self):
        super().__init__(); self.title(APP_NAME); self.geometry("1000x680"); self.minsize(900, 620)
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.events, self.stop_event = queue.Queue(), threading.Event(); self.worker = None
        global DATA_DIR, DB_PATH, CONFIG_PATH
        self.store, DATA_DIR = open_store(APP_DIR)
        DB_PATH, CONFIG_PATH = self.store.path, DATA_DIR / "wr503_app_config.json"
        self.views = {}
        self.analysis_after_id = None
        self.page, self.page_size = 0, 100; self.config_data = self.load_config()
        style = ttk.Style(self); style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Big.TLabel", font=("Segoe UI", 60, "bold"), anchor="center")
        style.configure("Muted.TLabel", foreground="#52616b")
        self.build_ui(); self.after(80, self.consume_events); self.after(1000, self.periodic_refresh); self.refresh_history()

    def load_config(self):
        defaults = {"host":"192.168.10.11","port":4000,"threshold":.3,"cutoff":200,"target_hz":512,"debounce_samples":3}
        for path in (CONFIG_PATH, BUNDLED_CONFIG_PATH):
            try: return {**defaults, **json.loads(path.read_text(encoding="utf-8"))}
            except Exception: continue
        return defaults
    def save_config(self):
        data = {"host":self.host.get(),"port":self.port.get(),"threshold":self.threshold.get(),"cutoff":self.cutoff.get(),"target_hz":self.target_hz.get(),"debounce_samples":self.debounce_samples.get()}
        try: CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError: pass

    def build_ui(self):
        tabs = ttk.Notebook(self); tabs.pack(fill="both", expand=True, padx=10, pady=10)
        history, analysis = ttk.Frame(tabs, padding=12), ttk.Frame(tabs, padding=12)
        tabs.add(history, text="  觀測歷史  "); tabs.add(analysis, text="  震動數據分析  ")
        self.build_history(history); self.build_analysis(analysis)

    def build_history(self, parent):
        parent.columnconfigure(0, weight=1); parent.rowconfigure(1, weight=1)
        box = ttk.LabelFrame(parent, text="RASeMIO 連線與擷取（5 Hz）", padding=10); box.grid(row=0,column=0,sticky="ew")
        box.columnconfigure(1, weight=1)
        controls=ttk.Frame(box); controls.grid(row=0,column=0,sticky="w",padx=(3,18))
        self.host=tk.StringVar(value=self.config_data["host"]); self.port=tk.StringVar(value=self.config_data["port"])
        for col,(label,var,width) in enumerate((("目標主機 IP",self.host,16),("Port",self.port,7))):
            ttk.Label(controls,text=label).grid(row=0,column=col*2,padx=(3,2)); ttk.Entry(controls,textvariable=var,width=width).grid(row=0,column=col*2+1,padx=(0,7))
        self.connection_btn=ttk.Button(controls,text="連線",command=self.toggle_connection,width=16); self.connection_btn.grid(row=0,column=4,padx=4)
        status_card=ttk.Frame(box,height=126); status_card.grid(row=1,column=0,columnspan=2,sticky="ew",padx=3,pady=(10,0)); status_card.grid_propagate(False)
        for column in range(6): status_card.columnconfigure(column,weight=1,uniform="status")
        for row in range(2): status_card.rowconfigure(row,weight=1,uniform="status")
        self.status_texts={key:tk.StringVar(value=f"{label}\n--") for label,key in self.STATUS_FIELDS}
        self.status_tiles={}
        self.last_status_values={}
        for index,(label,key) in enumerate(self.STATUS_FIELDS):
            tile=tk.Label(status_card,textvariable=self.status_texts[key],font=("Segoe UI",10,"bold"),bg="#9ca3af",fg="white",relief="flat")
            tile.grid(row=index//6,column=index%6,sticky="nsew",padx=(0 if index%6==0 else 3,0),pady=(0 if index//6==0 else 3,0))
            self.status_tiles[key]=tile
        preview=ttk.LabelFrame(parent,text="狀態歷史",padding=8); preview.grid(row=1,column=0,sticky="nsew",pady=(7,0)); preview.columnconfigure(0,weight=1); preview.rowconfigure(1,weight=1)
        filters=ttk.Frame(preview); filters.grid(row=0,column=0,sticky="ew",pady=(0,6)); self.history_start=tk.StringVar(); self.history_end=tk.StringVar(); self.history_field=tk.StringVar(value="全部資料")
        ttk.Label(filters,text="開始 UTC").pack(side="left")
        self.history_start_box=ttk.Combobox(filters,textvariable=self.history_start,width=21,state="disabled"); self.history_start_box.pack(side="left",padx=4)
        ttk.Label(filters,text="結束 UTC").pack(side="left")
        self.history_end_box=ttk.Combobox(filters,textvariable=self.history_end,width=21,state="disabled"); self.history_end_box.pack(side="left",padx=4)
        ttk.Label(filters,text="顯示").pack(side="left",padx=(8,0))
        self.history_field_box=ttk.Combobox(filters,textvariable=self.history_field,values=["全部資料",*self.HISTORY_FIELDS],width=23,state="readonly"); self.history_field_box.pack(side="left",padx=4)
        self.history_start_box.bind("<<ComboboxSelected>>",lambda _event:self.hour_selected("start"))
        self.history_end_box.bind("<<ComboboxSelected>>",lambda _event:self.hour_selected("end"))
        self.history_field_box.bind("<<ComboboxSelected>>",lambda _event:self.first_page())
        self._latest_history_hour=""; self.history_count=ttk.Label(filters); self.history_count.pack(side="right")
        table_frame=ttk.Frame(preview); table_frame.grid(row=1,column=0,sticky="nsew"); table_frame.columnconfigure(0,weight=1); table_frame.rowconfigure(0,weight=1)
        self.history_table=ttk.Treeview(table_frame,show="headings")
        self.history_table.grid(row=0,column=0,sticky="nsew")
        nav=ttk.Frame(preview); nav.grid(row=2,column=0,pady=5); ttk.Button(nav,text="上一頁",command=lambda:self.change_page(-1)).pack(side="left")
        self.page_label=ttk.Label(nav,text="第 1 頁"); self.page_label.pack(side="left",padx=8); ttk.Button(nav,text="下一頁",command=lambda:self.change_page(1)).pack(side="left")

    def build_analysis(self, parent):
        parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=1)
        box=ttk.LabelFrame(parent,text="分析設定",padding=8); box.grid(row=0,column=0,sticky="ew")
        self.threshold=tk.StringVar(value=self.config_data["threshold"]); self.cutoff=tk.StringVar(value=self.config_data["cutoff"]); self.target_hz=tk.StringVar(value=self.config_data["target_hz"]); self.debounce_samples=tk.StringVar(value=self.config_data["debounce_samples"])
        analysis_entries=[]
        for col,(label,var) in enumerate((("驗收標準 (g)",self.threshold),("低通 Hz",self.cutoff),("重採樣 Hz",self.target_hz),("防彈跳點數",self.debounce_samples))):
            ttk.Label(box,text=label).grid(row=1,column=col*2)
            entry=ttk.Entry(box,textvariable=var,width=8); entry.grid(row=1,column=col*2+1,padx=3); analysis_entries.append(entry)
        for entry in analysis_entries:
            entry.bind("<Return>",self.schedule_analysis); entry.bind("<FocusOut>",self.schedule_analysis)
        self.filter_values={
            "mode":{value:tk.BooleanVar() for value in range(4)},
            "motion_status":{value:tk.BooleanVar() for value in range(4)},
            "selected_gripper":{value:tk.BooleanVar() for value in range(4)},
        }
        self.filter_ranges={column:(tk.StringVar(),tk.StringVar()) for column in
                            ("ptp_speed_pct","linear_speed_mm_s","speed_ratio_pct","acceleration_ms","deceleration_ms")}
        self.filter_button=ttk.Button(box,text="分析資料篩選…",command=self.open_analysis_filters,state="disabled")
        self.filter_button.grid(row=1,column=8,padx=(12,4))
        self.filter_summary=tk.StringVar(value="沒有對時資料：使用完整 CSV")
        ttk.Label(box,textvariable=self.filter_summary,style="Muted.TLabel").grid(row=1,column=9,columnspan=4,sticky="w",padx=4)
        data_tabs=ttk.Notebook(parent); data_tabs.grid(row=1,column=0,sticky="nsew",pady=(5,0))
        candidate,baseline,tuning,diagnosis=(ttk.Frame(data_tabs,padding=5),ttk.Frame(data_tabs,padding=5),
                                   ttk.Frame(data_tabs,padding=5),ttk.Frame(data_tabs,padding=5))
        data_tabs.add(candidate,text="  Preview  "); data_tabs.add(baseline,text="  Benchmark  ")
        data_tabs.add(tuning,text="  Tuning Insights  ")
        data_tabs.add(diagnosis,text="  Diagnosis  ")
        self.build_analysis_view(candidate,"candidate",comparison=False)
        self.build_analysis_view(baseline,"baseline",comparison=True)
        self.build_tuning_view(tuning)
        self.build_diagnosis_view(diagnosis)

    def open_analysis_filters(self):
        dialog=tk.Toplevel(self); dialog.title("分析資料篩選"); dialog.transient(self); dialog.grab_set(); dialog.resizable(False,False)
        aligned_frames=[view["result"].aligned.loc[view["result"].aligned.covered] for view in self.views.values()
                        if view.get("result") is not None and view["result"].has_overlap]
        categorical=(
            ("Wafer 狀態 GPST","mode",((0,"無片"),(1,"上手有片"),(2,"下手有片"),(3,"雙手有片"))),
            ("動作狀態 GMST","motion_status",((0,"動作中"),(1,"停止"),(2,"暫停"),(3,"延遲"))),
            ("選定牙叉 GSID","selected_gripper",tuple((value,str(value)) for value in range(4))),
        )
        for row,(title,column,choices) in enumerate(categorical):
            frame=ttk.LabelFrame(dialog,text=title,padding=6); frame.grid(row=row,column=0,sticky="ew",padx=10,pady=(8 if row==0 else 2,2))
            available=set()
            for aligned in aligned_frames:
                if column in aligned:available.update(pd.to_numeric(aligned[column],errors="coerce").dropna().astype(int).unique())
            for index,(value,label) in enumerate(choices):
                if value not in available:self.filter_values[column][value].set(False)
                ttk.Checkbutton(frame,text=f"{value} {label}",variable=self.filter_values[column][value],state="normal" if value in available else "disabled").grid(row=0,column=index,padx=5,sticky="w")
        ranges=ttk.LabelFrame(dialog,text="數值範圍（留白代表不限；最小值與最大值皆包含）",padding=6); ranges.grid(row=3,column=0,sticky="ew",padx=10,pady=4)
        labels=(("ptp_speed_pct","點到點速度 (%)"),("linear_speed_mm_s","直線速度 (mm/s)"),("speed_ratio_pct","速度比例 (%)"),("acceleration_ms","加速時間 (ms)"),("deceleration_ms","減速時間 (ms)"))
        for row,(column,label) in enumerate(labels):
            minimum,maximum=self.filter_ranges[column]; ttk.Label(ranges,text=label,width=20).grid(row=row,column=0,sticky="w")
            pieces=[pd.to_numeric(aligned[column],errors="coerce") for aligned in aligned_frames if column in aligned]
            observed=pd.concat(pieces,ignore_index=True).dropna() if pieces else pd.Series(dtype=float)
            state="normal" if not observed.empty else "disabled"
            if observed.empty:minimum.set("");maximum.set("")
            ttk.Label(ranges,text="最小").grid(row=row,column=1); ttk.Entry(ranges,textvariable=minimum,width=10,state=state).grid(row=row,column=2,padx=3)
            ttk.Label(ranges,text="最大").grid(row=row,column=3); ttk.Entry(ranges,textvariable=maximum,width=10,state=state).grid(row=row,column=4,padx=3)
            ttk.Label(ranges,text=(f"資料 {observed.min():g}–{observed.max():g}" if not observed.empty else "無資料"),style="Muted.TLabel").grid(row=row,column=5,padx=(6,0),sticky="w")
        actions=ttk.Frame(dialog); actions.grid(row=4,column=0,pady=9)
        def clear():
            for values in self.filter_values.values():
                for variable in values.values():variable.set(False)
            for minimum,maximum in self.filter_ranges.values():minimum.set("");maximum.set("")
        def apply():
            try:self.active_analysis_filters()
            except ValueError as exc:messagebox.showerror(APP_NAME,str(exc),parent=dialog);return
            dialog.destroy();self.run_analysis()
        ttk.Button(actions,text="清除全部",command=clear).pack(side="left",padx=4);ttk.Button(actions,text="套用",command=apply).pack(side="left",padx=4)

    def active_analysis_filters(self):
        filters={}; descriptions=[]
        names={"mode":"GPST","motion_status":"GMST","selected_gripper":"GSID"}
        for column,variables in self.filter_values.items():
            values={value for value,variable in variables.items() if variable.get()}
            if values:filters[column]={"values":values};descriptions.append(f"{names[column]}={','.join(map(str,sorted(values)))}")
        range_names={"ptp_speed_pct":"點到點速度","linear_speed_mm_s":"直線速度","speed_ratio_pct":"速度比例","acceleration_ms":"加速時間","deceleration_ms":"減速時間"}
        for column,(minimum_text,maximum_text) in self.filter_ranges.items():
            minimum=float(minimum_text.get()) if minimum_text.get().strip() else None; maximum=float(maximum_text.get()) if maximum_text.get().strip() else None
            if minimum is not None and maximum is not None and minimum>maximum:raise ValueError(f"{range_names[column]}最小值不可大於最大值")
            if minimum is not None or maximum is not None:
                filters[column]={"minimum":minimum,"maximum":maximum};descriptions.append(f"{range_names[column]} {minimum if minimum is not None else '不限'}–{maximum if maximum is not None else '不限'}")
        self.filter_summary.set("篩選："+"；".join(descriptions) if descriptions else "沒有篩選：使用完整 CSV")
        return filters

    def build_analysis_view(self,parent,role,comparison):
        parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=1)
        header=ttk.Frame(parent); header.grid(row=0,column=0,sticky="ew",pady=(0,5))
        import_text="匯入 Candidate CSV" if role=="candidate" else "匯入 Baseline CSV"
        ttk.Button(header,text=import_text,command=lambda:self.import_sensor(role)).pack(side="left")
        info=tk.StringVar(value="尚未匯入"); ttk.Label(header,textvariable=info,style="Muted.TLabel").pack(side="left",padx=8)
        offset=tk.StringVar(value="0.000")
        ttk.Button(header,text="偏移歸零",command=lambda:self.set_status_offset(role,0.0)).pack(side="right")
        offset_entry=ttk.Entry(header,textvariable=offset,width=8); offset_entry.pack(side="right",padx=(3,4))
        offset_entry.bind("<Return>",lambda _event:self.apply_status_offset_entry(role)); offset_entry.bind("<FocusOut>",lambda _event:self.apply_status_offset_entry(role))
        ttk.Label(header,text="GPST 對時偏移 (s)").pack(side="right")
        ttk.Label(header,text="在圖上左鍵拖曳藍色遮罩對齊有片／無片",style="Muted.TLabel").pack(side="right",padx=(0,10))
        pane=ttk.Frame(parent); pane.grid(row=1,column=0,sticky="nsew")
        pane.columnconfigure(0,weight=4,uniform="analysis_width"); pane.columnconfigure(1,weight=6,uniform="analysis_width"); pane.rowconfigure(0,weight=1)
        plots,metrics=ttk.Frame(pane),ttk.Frame(pane); plots.grid(row=0,column=0,sticky="nsew"); metrics.grid(row=0,column=1,sticky="nsew",padx=(6,0))
        figure=Figure(figsize=(8,6),dpi=100,constrained_layout=True); canvas=FigureCanvasTkAgg(figure,plots); canvas.get_tk_widget().pack(fill="both",expand=True)
        table=ttk.Treeview(metrics,columns=("category","metric","X","Y","Z"),show="headings")
        for c,text in (("category","分類"),("metric","Metric"),("X","X"),("Y","Y"),("Z","Z")): table.heading(c,text=text)
        table.tag_configure("percentage",font=("Segoe UI",9,"bold")); table.tag_configure("better",foreground="#15803d"); table.tag_configure("worse",foreground="#b91c1c"); table.pack(fill="both",expand=True)
        self.views[role]={"info":info,"sensor":None,"start":None,"result":None,"figure":figure,"canvas":canvas,
                          "table":table,"comparison":comparison,"range":None,"full_range":None,"axes":None,
                          "scroll_cid":None,"metrics":None,"offset":offset,"applied_offset":0.0,
                          "observations":None,"mask_patches":[],"drag":None,"drag_cids":[],"xlim":None}

    def build_tuning_view(self,parent):
        parent.columnconfigure(0,weight=4,uniform="tuning_width"); parent.columnconfigure(1,weight=6,uniform="tuning_width"); parent.rowconfigure(1,weight=1)
        self.tuning_status=tk.StringVar(value="請先匯入 Candidate；匯入 Baseline 後可顯示差異")
        controls=ttk.Frame(parent); controls.grid(row=0,column=0,columnspan=2,sticky="ew",pady=(0,5))
        ttk.Label(controls,text="頻帶：低頻 0.5–10 Hz｜中頻 10–50 Hz｜高頻 50 Hz–低通截止頻率",style="Muted.TLabel").pack(side="left")
        self.tuning_view_mode=tk.StringVar(value="PSD 頻譜")
        ttk.Label(controls,text="圖形").pack(side="left",padx=(18,3))
        view_box=ttk.Combobox(controls,textvariable=self.tuning_view_mode,values=("PSD 頻譜","PSD 改善／惡化","STFT 時間—頻率"),state="readonly",width=16); view_box.pack(side="left")
        self.spectrogram_axis=tk.StringVar(value="X")
        ttk.Label(controls,text="軸向").pack(side="left",padx=(10,3))
        self.tuning_axis_box=ttk.Combobox(controls,textvariable=self.spectrogram_axis,values=("X","Y","Z"),state="readonly",width=4); self.tuning_axis_box.pack(side="left")
        self.tuning_min_hz=tk.StringVar(value="0.5"); self.tuning_max_hz=tk.StringVar(value="200")
        self.tuning_peak_count=tk.StringVar(value="5"); self.tuning_window_s=tk.StringVar(value="2"); self.tuning_overlap_pct=tk.StringVar(value="75")
        self.tuning_parameter_widgets=[]
        for label,variable,width in (("最低 Hz",self.tuning_min_hz,6),("最高 Hz",self.tuning_max_hz,6),("峰值數",self.tuning_peak_count,4),("時間窗 s",self.tuning_window_s,5),("重疊 %",self.tuning_overlap_pct,5)):
            label_widget=ttk.Label(controls,text=label); label_widget.pack(side="left",padx=(7,2))
            entry=ttk.Entry(controls,textvariable=variable,width=width); entry.pack(side="left"); entry.bind("<Return>",lambda _event:self.render_tuning()); entry.bind("<FocusOut>",lambda _event:self.render_tuning())
            self.tuning_parameter_widgets.append((label,label_widget,entry))
        view_box.bind("<<ComboboxSelected>>",lambda _event:self.on_tuning_view_changed()); self.tuning_axis_box.bind("<<ComboboxSelected>>",lambda _event:self.render_tuning())
        figure=Figure(figsize=(8,6),dpi=100,constrained_layout=True); canvas=FigureCanvasTkAgg(figure,parent); canvas.get_tk_widget().grid(row=1,column=0,sticky="nsew")
        right=ttk.Frame(parent); right.grid(row=1,column=1,sticky="nsew",padx=(6,0)); right.rowconfigure(1,weight=1); right.columnconfigure(0,weight=1)
        ttk.Label(right,textvariable=self.tuning_status,wraplength=560,style="Muted.TLabel").grid(row=0,column=0,sticky="ew",pady=(0,4))
        table=ttk.Treeview(right,columns=("kind","item","X","Y","Z"),show="headings")
        for column,text in (("kind","分類"),("item","診斷項目"),("X","X"),("Y","Y"),("Z","Z")):table.heading(column,text=text)
        table.tag_configure("percentage",font=("Segoe UI",9,"bold"))
        table.tag_configure("better",foreground="#15803d"); table.tag_configure("worse",foreground="#b91c1c"); table.grid(row=1,column=0,sticky="nsew")
        self.tuning={"figure":figure,"canvas":canvas,"table":table,"motion_cid":None}
        self.on_tuning_view_changed(render=False)

    def on_tuning_view_changed(self,render=True):
        is_stft=self.tuning_view_mode.get()=="STFT 時間—頻率"
        if is_stft:
            if self.spectrogram_axis.get() not in ("X","Y","Z"):self.spectrogram_axis.set("X")
            self.tuning_axis_box.config(state="readonly")
        else:
            self.spectrogram_axis.set(""); self.tuning_axis_box.config(state="disabled")
        for name,label,entry in self.tuning_parameter_widgets:
            visible=(name in {"最低 Hz","最高 Hz"} or (is_stft and name in {"時間窗 s","重疊 %"}) or (not is_stft and name=="峰值數"))
            if visible:
                label.pack(side="left",padx=(7,2)); entry.pack(side="left")
            else:
                label.pack_forget(); entry.pack_forget()
        if render:self.render_tuning()

    def build_diagnosis_view(self,parent):
        parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=3); parent.rowconfigure(2,weight=2)
        controls=ttk.Frame(parent); controls.grid(row=0,column=0,sticky="ew",pady=(0,5))
        ttk.Label(controls,text="ISO 10816 速度振動參考（不等同 WR503 驗收標準）").pack(side="left")
        self.diagnosis_method=tk.StringVar(value="ISO 10816")
        self.diagnosis_params={}; self.diagnosis_param_frames={}
        parameter_specs={
            "ISO 10816":(("最低 Hz","iso_min","10",7),("最高 Hz","iso_max","200",7),("A/B","iso_ab","0.71",6),("B/C","iso_bc","1.8",6),("C/D","iso_cd","4.5",6)),
        }
        for method,specs in parameter_specs.items():
            frame=ttk.Frame(controls); frame.pack(side="left",padx=(8,0)); self.diagnosis_param_frames[method]=frame
            for label,key,default,width in specs:
                ttk.Label(frame,text=label).pack(side="left",padx=(5,2)); variable=tk.StringVar(value=default); self.diagnosis_params[key]=variable
                widget=ttk.Entry(frame,textvariable=variable,width=width); widget.bind("<Return>",lambda _event:self.render_diagnostic_tool()); widget.bind("<FocusOut>",lambda _event:self.render_diagnostic_tool())
                widget.pack(side="left")
        self.diagnosis_status=tk.StringVar(value="請先在 Preview 匯入 Candidate CSV")
        ttk.Label(controls,textvariable=self.diagnosis_status,style="Muted.TLabel").pack(side="right",padx=8)
        tool_pane=ttk.Frame(parent); tool_pane.grid(row=1,column=0,sticky="nsew"); tool_pane.columnconfigure(0,weight=3); tool_pane.columnconfigure(1,weight=2); tool_pane.rowconfigure(0,weight=1)
        tool_figure=Figure(figsize=(7,4),dpi=100,constrained_layout=True); tool_canvas=FigureCanvasTkAgg(tool_figure,tool_pane); tool_canvas.get_tk_widget().grid(row=0,column=0,sticky="nsew")
        tool_table=ttk.Treeview(tool_pane,show="headings"); tool_table.grid(row=0,column=1,sticky="nsew",padx=(6,0))
        details=ttk.Notebook(parent); details.grid(row=2,column=0,sticky="nsew",pady=(7,0)); event_frame=ttk.Frame(details); evidence_frame=ttk.Frame(details); details.add(event_frame,text="超標事件"); details.add(evidence_frame,text="根因線索")
        event_frame.columnconfigure(0,weight=1); event_frame.rowconfigure(0,weight=1); evidence_frame.columnconfigure(0,weight=1); evidence_frame.rowconfigure(0,weight=1)
        events=ttk.Treeview(event_frame,columns=("time","axis","peak","duration","gpst","gmst","gsid","position"),show="headings",height=6)
        for column,text,width in (("time","時間 (s)",90),("axis","軸",45),("peak","峰值 (g)",90),("duration","持續 (ms)",90),("gpst","GPST",60),("gmst","GMST",60),("gsid","GSID",60),("position","命令位置",260)):
            events.heading(column,text=text); events.column(column,width=width,anchor="w" if column=="position" else "center",stretch=column=="position")
        events.grid(row=0,column=0,sticky="nsew")
        evidence=ttk.Treeview(evidence_frame,columns=("source","evidence","strength","next"),show="headings",height=6)
        for column,text,width in (("source","可能來源",120),("evidence","目前證據",360),("strength","線索",70),("next","下一個單變因驗證",420)):
            evidence.heading(column,text=text); evidence.column(column,width=width,anchor="w",stretch=column in {"evidence","next"})
        evidence.grid(row=0,column=0,sticky="nsew")
        self.diagnosis={"events":events,"evidence":evidence,"figure":tool_figure,"canvas":tool_canvas,"table":tool_table}
        self.show_diagnosis_params(render=False)

    def show_diagnosis_params(self,render=True):
        selected=self.diagnosis_method.get()
        for method,frame in self.diagnosis_param_frames.items():
            if method==selected:frame.pack(side="left",padx=(8,0))
            else:frame.pack_forget()
        if render:self.render_diagnostic_tool()

    def settings(self):
        host = self.host.get().strip()
        adapter = matching_adapter(host)
        return {"host":host,"port":int(self.port.get()),"local_ip":adapter.address,"adapter":adapter}
    def show_disconnected(self):
        labels={key:label for label,key in self.STATUS_FIELDS}
        for key,tile in self.status_tiles.items():
            self.status_texts[key].set(f"{labels[key]}\n--"); tile.config(bg="#9ca3af")
        self.last_status_values.clear()
    @staticmethod
    def compact_status(value):
        if value is None: return "--"
        if isinstance(value,float): return f"{value:g}"
        text=str(value)
        return text if len(text)<=24 else text[:21]+"..."
    def update_status_tiles(self,payload):
        missing=object()
        for label,key in self.STATUS_FIELDS:
            value=payload[key]; shown=self.compact_status(value)
            if key in self.CATEGORICAL_STATUS:
                meanings,colors=self.CATEGORICAL_STATUS[key]
                meaning=meanings.get(value)
                if meaning: shown=f"{shown}｜{meaning}"
                color=colors.get(value,"#16a34a")
            elif key in self.SETTING_STATUS:
                previous=self.last_status_values.get(key,missing)
                changed=previous is not missing and previous!=value
                color="#f59e0b" if changed else "#16a34a"
                if changed:
                    self.after(600,lambda field=key,current=value:self.settle_setting_tile(field,current))
            else:
                color="#16a34a"
            self.status_texts[key].set(f"{label}\n{shown}")
            self.status_tiles[key].config(bg=color)
            self.last_status_values[key]=value
    def settle_setting_tile(self,key,value):
        if self.last_status_values.get(key)==value and self.worker and self.worker.is_alive():
            self.status_tiles[key].config(bg="#16a34a")
    def toggle_connection(self):
        if self.worker and self.worker.is_alive(): self.stop_event.set(); self.connection_btn.config(state="disabled"); return
        try: cfg=self.settings()
        except (ValueError, OSError) as exc: messagebox.showerror(APP_NAME,f"連線參數錯誤：{exc}"); return
        self.save_config()
        self.stop_event = threading.Event()
        self.worker = Acquisition(self.events, self.stop_event, cfg, DB_PATH)
        self.worker.start()
        self.connection_btn.config(text="連線中／中斷")
        adapter = cfg["adapter"]
        self.active_adapter_text = f"{adapter.name}：{adapter.address} / {adapter.netmask}"
        self.show_disconnected()
    def consume_events(self):
        try:
            while True:
                kind,payload=self.events.get_nowait()
                if kind=="sample":
                    self.update_status_tiles(payload)
                    self.connection_btn.config(text="已連線／中斷")
                elif kind=="connected": self.connection_btn.config(text="已連線／中斷")
                elif kind=="retry": self.show_disconnected(); self.connection_btn.config(text="重試中／中斷")
                elif kind=="stopped": self.worker=None; self.show_disconnected(); self.connection_btn.config(text="連線",state="normal")
        except queue.Empty: pass
        self.after(80,self.consume_events)
    def first_page(self): self.page=0; self.refresh_history()
    def change_page(self,delta): self.page=max(0,self.page+delta); self.refresh_history()
    def hour_selected(self, changed):
        if self.history_start.get() > self.history_end.get():
            if changed == "start": self.history_end.set(self.history_start.get())
            else: self.history_start.set(self.history_end.get())
        self.first_page()
    def update_history_hours(self):
        labels=[f"{hour.replace('T',' ')}:00 UTC" for hour in self.store.available_hours()]
        previous_latest=self._latest_history_hour; follow_latest=not self.history_end.get() or self.history_end.get()==previous_latest
        self._latest_history_hour=labels[-1] if labels else ""
        for box in (self.history_start_box,self.history_end_box):
            box.config(values=labels,state="readonly" if labels else "disabled")
        if not labels:
            self.history_start.set(""); self.history_end.set(""); return
        if self.history_start.get() not in labels: self.history_start.set(labels[0])
        if follow_latest or self.history_end.get() not in labels: self.history_end.set(labels[-1])
    @staticmethod
    def hour_bounds(label):
        hour=datetime.strptime(label,"%Y-%m-%d %H:00 UTC").replace(tzinfo=timezone.utc)
        return utc_text(hour),utc_text(hour+timedelta(hours=1)-timedelta(microseconds=1))
    def refresh_history(self):
        self.update_history_hours()
        start,end=("","") if not self.history_start.get() else (self.hour_bounds(self.history_start.get())[0],self.hour_bounds(self.history_end.get())[1])
        selected = self.history_field.get()
        fields = list(self.HISTORY_FIELDS.items()) if selected == "全部資料" else [(selected, self.HISTORY_FIELDS[selected])]
        field_names=tuple(definition[0] for _,definition in fields)
        page=self.store.query_values(fields=field_names,limit=self.page_size,offset=self.page*self.page_size,start=start,end=end)
        labels={definition[0]:f"{label}（{definition[1]}）" for label,definition in fields}
        self.history_table.configure(columns=("timestamp","field","value"))
        for column,text,width,anchor in (("timestamp","時間",220,"w"),("field","欄位",220,"w"),("value","值",420,"w")):
            self.history_table.heading(column,text=text); self.history_table.column(column,width=width,anchor=anchor,stretch=column=="value")
        self.history_table.delete(*self.history_table.get_children())
        for row in page.rows:
            self.history_table.insert("","end",values=(row.timestamp,labels[row.field],row.value))
        pages=max(1,(page.total+self.page_size-1)//self.page_size); self.page=min(self.page,pages-1); self.page_label.config(text=f"第 {self.page+1}/{pages} 頁"); self.history_count.config(text=f"共 {page.total:,} 筆")
    def periodic_refresh(self):
        if self.page==0: self.refresh_history()
        self.after(1000,self.periodic_refresh)
    def import_sensor(self):
        path=filedialog.askopenfilename(filetypes=[("Sensor CSV","*.csv")])
        if not path:return
        try:
            self.sensor=load_sensor_csv(Path(path)); self.sensor_path=Path(path); hz=infer_source_hz(self.sensor); meta=self.sensor.attrs.get("metadata",{}); date_value=next((v for k,v in meta.items() if "date" in k.lower()),"")
            if date_value:
                parsed=pd.to_datetime(date_value).to_pydatetime(); parsed=parsed.astimezone() if parsed.tzinfo is None else parsed; self.sensor_start.set(parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
            start_text=self.sensor_start.get() or "無法辨識"
            self.sensor_info.set(f"{Path(path).name}｜{len(self.sensor):,} samples｜{hz:.3f} Hz｜{self.sensor.time_s.iloc[-1]:.3f} s｜開始時間 {start_text}"); self.run_analysis()
        except Exception as exc: messagebox.showerror(APP_NAME,f"CSV 匯入失敗：{exc}")
    def parse_start(self):
        value=datetime.fromisoformat(self.sensor_start.get().strip()); return value.astimezone() if value.tzinfo is None else value
    def schedule_analysis(self,_event=None):
        if self.sensor is None:return
        if self.analysis_after_id: self.after_cancel(self.analysis_after_id)
        self.analysis_after_id=self.after(150,self.run_scheduled_analysis)
    def run_scheduled_analysis(self):
        self.analysis_after_id=None; self.run_analysis()
    def run_analysis(self):
        if self.sensor is None:
            return
        try:
            start = self.parse_start()
            end = start + pd.Timedelta(seconds=float(self.sensor.time_s.iloc[-1]))
            observations = self.store.interval_frame(utc_text(start), utc_text(end))
            selected = {mode for mode, value in self.mode_vars.items() if value.get()}
            self.result = analyze_recording(
                self.sensor, start, observations, selected,
                ProcessingSettings(float(self.cutoff.get()), float(self.target_hz.get()), float(self.threshold.get())),
                POLLING_INTERVAL_S,
            )
            self.save_config()
            for box in self.mode_checks:
                box.config(state="normal" if self.result.has_overlap else "disabled")
            if not self.result.has_overlap:
                for value in self.mode_vars.values():
                    value.set(False)
            self.render_result()
        except Exception as exc:
            messagebox.showerror(APP_NAME,f"無法分析：{exc}")

    @staticmethod
    def runs(mask):
        edges=np.diff(np.r_[False,mask,False].astype(np.int8)); return list(zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1)))
    def render_result(self):
        result=self.result; self.figure.clear(); axes=self.figure.subplots(3,1,sharex=True); self.plot_axes=axes; aligned=result.aligned
        for name,axis,color in zip("XYZ",axes,("#315a7d","#c45a3c","#4f7c59")):
            axis.plot(aligned.time_s,aligned[name],color="#9ca3af",alpha=.35,lw=.45)
            for _, segment in result.processed.groupby("segment",sort=False):
                axis.plot(segment.time_s,segment[name],color=color,lw=.75)
            for begin,end in self.runs((~aligned.covered).to_numpy(bool)):
                right=max(begin,end-1); axis.axvspan(aligned.time_s.iloc[begin],aligned.time_s.iloc[right],color="#d1d5db",alpha=.7)
            threshold=float(self.threshold.get()); axis.axhline(threshold,color="red",ls="--",lw=.8); axis.axhline(-threshold,color="red",ls="--",lw=.8); axis.set_ylabel(f"{name} (g)"); axis.grid(alpha=.2)
        axes[-1].set_xlabel("Sensor time (s)")
        self.full_time_range=(float(aligned.time_s.min()),float(aligned.time_s.max()))
        self.analysis_range=self.full_time_range
        self.range_selectors=[]
        for axis in axes:
            selector=SpanSelector(axis,self.on_range_selected,"horizontal",useblit=True,interactive=True,
                                  drag_from_anywhere=True,props={"facecolor":"#2563eb","alpha":.14})
            selector.extents=self.full_time_range; self.range_selectors.append(selector)
        if hasattr(self,"plot_scroll_cid"): self.canvas.mpl_disconnect(self.plot_scroll_cid)
        self.plot_scroll_cid=self.canvas.mpl_connect("scroll_event",self.on_plot_scroll)
        self.update_shared_y_limits(); self.canvas.draw(); self.render_metrics(result.metrics)
    def render_metrics(self, metrics):
        self.range_metrics=metrics; self.metric_table.delete(*self.metric_table.get_children())
        labels=(("RMS (g)","RMS_g",".6f",""),("P99 |g|","P99_abs_g",".6f",""),("P99.9 |g|","P99.9_abs_g",".6f",""),("Max |g|","Max_abs_g",".6f",""),("規格內 樣本/總數","within","",""),("規格內比例","within_pct",".3f","percentage"),("超標 樣本/總數","exceed","",""),("超標比例","exceed_pct",".3f","percentage"),("超標事件數","event_count",".0f",""),("有效秒數","analyzed_seconds",".3f",""),("事件/分鐘","events_per_min",".3f",""),("最長事件 ms","max_event_ms",".3f",""))
        for label,key,fmt,tag in labels:
            values=[]
            for name in "XYZ":
                row=metrics.loc[name]
                if key=="within":value=f"{int(row.within_samples)}/{int(row.total_samples)}"
                elif key=="exceed":value=f"{int(row.exceed_samples)}/{int(row.total_samples)}"
                elif key in ("within_pct","exceed_pct"):value=f"{row[key]:.3f}%"
                else:value=format(row[key],fmt)
                values.append(value)
            self.metric_table.insert("","end",values=(label,*values),tags=(tag,) if tag else ())
    def on_range_selected(self,start,end):
        if not self.result or abs(end-start)<=0:return
        low=max(self.full_time_range[0],min(start,end)); high=min(self.full_time_range[1],max(start,end))
        self.analysis_range=(low,high)
        for selector in self.range_selectors: selector.extents=(low,high)
        metrics=metrics_for_range(self.result.processed,low,high,float(self.target_hz.get()),float(self.threshold.get()))
        self.render_metrics(metrics); self.canvas.draw_idle()
    def on_plot_scroll(self,event):
        if not self.result or event.inaxes not in self.plot_axes or event.xdata is None:return
        left,right=self.plot_axes[-1].get_xlim(); factor=.8 if event.button=="up" else 1.25
        new_left=event.xdata-(event.xdata-left)*factor; new_right=event.xdata+(right-event.xdata)*factor
        full_left,full_right=self.full_time_range; width=min(new_right-new_left,full_right-full_left)
        new_left=max(full_left,min(new_left,full_right-width)); new_right=new_left+width
        self.plot_axes[-1].set_xlim(new_left,new_right); self.update_shared_y_limits(); self.canvas.draw_idle()
    def update_shared_y_limits(self):
        if not self.result:return
        left,right=self.plot_axes[-1].get_xlim(); visible=self.result.aligned.loc[self.result.aligned.time_s.between(left,right),["X","Y","Z"]]
        if visible.empty:return
        threshold=float(self.threshold.get()); low=min(float(visible.min().min()),-threshold); high=max(float(visible.max().max()),threshold)
        padding=max((high-low)*.05,.01); limits=(low-padding,high+padding)
        for axis in self.plot_axes: axis.set_ylim(limits)
    def export_aligned(self):
        if self.result:
            path=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")]); selected=self.result.aligned.loc[self.result.aligned.time_s.between(*self.analysis_range)]; selected.to_csv(path,index=False,encoding="utf-8-sig") if path else None

    # Candidate/Baseline analysis boundary. These definitions supersede the
    # original single-recording handlers above while migration remains local.
    def import_sensor(self,role):
        path=filedialog.askopenfilename(filetypes=[("Sensor CSV","*.csv")])
        if not path:return
        view=self.views[role]
        try:
            sensor_path=Path(path); sensor=load_sensor_csv(sensor_path); hz=infer_source_hz(sensor); meta=sensor.attrs.get("metadata",{})
            start=infer_sensor_start(sensor_path,meta)
            view.update(sensor=sensor,start=start,path=Path(path),applied_offset=0.0,xlim=None); view["offset"].set("0.000")
            start_text=start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if start else "無法辨識"
            view["info"].set(f"{Path(path).name}｜{len(sensor):,} samples｜{hz:.3f} Hz｜{sensor.time_s.iloc[-1]:.3f} s｜開始時間 {start_text}")
            self.run_analysis(role)
        except Exception as exc: messagebox.showerror(APP_NAME,f"CSV 匯入失敗：{exc}")

    def schedule_analysis(self,_event=None):
        if not any(view["sensor"] is not None for view in self.views.values()):return
        if self.analysis_after_id:self.after_cancel(self.analysis_after_id)
        self.analysis_after_id=self.after(150,self.run_scheduled_analysis)

    def run_scheduled_analysis(self):
        self.analysis_after_id=None; self.run_analysis()

    def run_analysis(self,role=None):
        roles=(role,) if role else tuple(self.views)
        try:
            settings=ProcessingSettings(float(self.cutoff.get()),float(self.target_hz.get()),float(self.threshold.get()),int(self.debounce_samples.get()))
            filters=self.active_analysis_filters()
            selected_modes=set(filters.get("mode",{}).get("values",set()))
            for current in roles:
                view=self.views[current]
                if view["sensor"] is None:continue
                if view["start"] is None:raise ValueError(f"{current.title()} CSV 無法辨識開始時間")
                end=view["start"]+pd.Timedelta(seconds=float(view["sensor"].time_s.iloc[-1]))
                # Widen the status query so a shifted GPST timeline can still reach the recording edges.
                margin=pd.Timedelta(seconds=abs(view["applied_offset"])+3*POLLING_INTERVAL_S)
                observations=self.store.diagnostic_interval_frame(utc_text(view["start"]-margin),utc_text(end+margin))
                view["observations"]=observations
                view["result"]=analyze_recording(view["sensor"],view["start"],observations,selected_modes,settings,POLLING_INTERVAL_S,filters,view["applied_offset"])
                self.render_result(current)
            has_overlap=any(view["result"] is not None and view["result"].has_overlap for view in self.views.values())
            self.filter_button.config(state="normal" if has_overlap else "disabled")
            if not has_overlap:
                self.filter_summary.set("沒有對時資料：使用完整 CSV")
            self.save_config(); self.render_comparison(); self.render_tuning(); self.render_diagnosis()
        except Exception as exc:messagebox.showerror(APP_NAME,f"無法分析：{exc}")

    def render_result(self,role):
        view=self.views[role]; result=view["result"]; figure=view["figure"]; canvas=view["canvas"]
        figure.clear(); axes=figure.subplots(3,1,sharex=True); aligned=result.aligned; view["axes"]=axes
        threshold=float(self.threshold.get())
        intervals=status_mask_intervals(view["observations"],view["start"],POLLING_INTERVAL_S,view["applied_offset"]) if view["observations"] is not None else []
        view["mask_patches"]=[]
        for name,axis,color in zip("XYZ",axes,("#315a7d","#c45a3c","#4f7c59")):
            axis.set_facecolor(self.NO_STATUS_COLOR)  # no RASeMIO status: stays gray where no mask is drawn
            for begin,end,value in intervals:
                patch=axis.axvspan(begin,end,color=self.mask_color(value),alpha=.55 if value==0 else .35,lw=0,zorder=0)
                view["mask_patches"].append((patch,begin))
            axis.plot(aligned.time_s,aligned[name],color="#9ca3af",alpha=.35,lw=.45)
            for _,segment in result.processed.groupby("segment",sort=False):axis.plot(segment.time_s,segment[name],color=color,lw=.75)
            axis.axhline(threshold,color="red",ls="--",lw=.8); axis.axhline(-threshold,color="red",ls="--",lw=.8)
            axis.set_ylabel(f"{name} (g)"); axis.grid(alpha=.2)
        if intervals:
            from matplotlib.patches import Patch
            axes[0].legend(handles=[Patch(color=self.mask_color(0),alpha=.55,label="GPST=0 無片"),
                                    Patch(color=self.mask_color(1),alpha=.35,label="GPST≥1 有片")],
                           loc="upper right",fontsize=7,framealpha=.85)
        axes[-1].set_xlabel("Sensor time (s)"); full=(float(aligned.time_s.min()),float(aligned.time_s.max()))
        view["full_range"]=full; view["range"]=full
        if view["xlim"]:axes[-1].set_xlim(*view["xlim"])
        for cid in [view["scroll_cid"],*view["drag_cids"]]:
            if cid:canvas.mpl_disconnect(cid)
        view["scroll_cid"]=canvas.mpl_connect("scroll_event",lambda event,r=role:self.on_plot_scroll(r,event))
        view["drag_cids"]=[canvas.mpl_connect("button_press_event",lambda event,r=role:self.on_mask_press(r,event)),
                           canvas.mpl_connect("motion_notify_event",lambda event,r=role:self.on_mask_drag(r,event)),
                           canvas.mpl_connect("button_release_event",lambda event,r=role:self.on_mask_release(r,event))]
        view["drag"]=None; view["metrics"]=result.metrics; self.update_shared_y_limits(role); canvas.draw()
        if role=="candidate":self.render_metrics(view["table"],result.metrics)
        else:self.render_comparison()

    @staticmethod
    def metric_rows():
        return (
            ("驗收條件","判定門檻值 (g)","threshold_g",".3f",""),
            ("","有效分析時間 (s)","analyzed_seconds",".3f",""),
            ("震動程度","RMS (g)","RMS_g",".6f",""),
            ("","最大峰值 |g|","Max_abs_g",".6f",""),
            ("","P99 |g|","P99_abs_g",".6f",""),
            ("","P99.9 |g|","P99.9_abs_g",".6f",""),
            ("驗收結果","軸向判定","verdict","",""),
            ("","最大值距門檻 (g)","limit_margin_g","+.6f",""),
            ("","超標 樣本/總數","exceed","",""),
            ("","超標比例","exceed_pct",".3f","percentage"),
            ("超標事件","獨立超標事件數","event_count",".0f",""),
            ("","事件率 (次/分鐘)","events_per_min",".3f",""),
            ("","最長事件 (ms)","max_event_ms",".3f",""),
            ("","累計超標時間 (ms)","total_excursion_ms",".3f",""),
        )

    @staticmethod
    def metric_value(row,key,fmt):
        if key=="within":return f"{int(row.within_samples)}/{int(row.total_samples)}"
        if key=="exceed":return f"{int(row.exceed_samples)}/{int(row.total_samples)}"
        if key=="verdict":return "PASS" if int(row.exceed_samples)==0 else "WARNING" if int(row.event_count)==0 else "FAIL"
        if key in ("within_pct","exceed_pct"):return f"{row[key]:.3f}%"
        return format(row[key],fmt)

    def render_metrics(self,table,metrics):
        table.delete(*table.get_children())
        for category,label,key,fmt,tag in self.metric_rows():
            values=[self.metric_value(metrics.loc[axis],key,fmt) for axis in "XYZ"]
            table.insert("","end",values=(category,label,*values),tags=(tag,) if tag else ())
        self.autosize_metric_columns(table)

    def render_comparison(self):
        baseline=self.views.get("baseline"); candidate=self.views.get("candidate")
        if not baseline:return
        table=baseline["table"]; table.delete(*table.get_children())
        if not candidate or candidate["metrics"] is None or baseline["metrics"] is None:
            table.insert("","end",values=("","請先匯入 Candidate 與 Baseline","","","")); self.autosize_metric_columns(table); return
        candidate_seconds=float(candidate["metrics"].loc["X","analyzed_seconds"]); baseline_seconds=float(baseline["metrics"].loc["X","analyzed_seconds"])
        duration_delta=abs(candidate_seconds-baseline_seconds)/max(candidate_seconds,baseline_seconds,1e-12)
        gate="可比較" if duration_delta<=.05 else f"注意：有效時間相差 {duration_delta*100:.1f}%"
        table.insert("","end",values=("可比性","有效分析時間",gate,gate,gate),tags=("better" if duration_delta<=.05 else "worse",))
        lower_better={"RMS_g","Max_abs_g","P99_abs_g","P99.9_abs_g","exceed","exceed_pct","event_count","events_per_min","max_event_ms","total_excursion_ms"}
        higher_better={"limit_margin_g"}
        for category,label,key,fmt,_tag in self.metric_rows():
            if key not in lower_better|higher_better:continue
            candidate_values=candidate["metrics"]; baseline_values=baseline["metrics"]
            differences=[]; finite_changes=[]
            for axis in "XYZ":
                if key=="exceed":
                    c=float(candidate_values.loc[axis,"exceed_samples"]); b=float(baseline_values.loc[axis,"exceed_samples"])
                    c_total=int(candidate_values.loc[axis,"total_samples"]); b_total=int(baseline_values.loc[axis,"total_samples"])
                else:c=float(candidate_values.loc[axis,key]); b=float(baseline_values.loc[axis,key])
                if not np.isfinite(b) or b==0:
                    differences.append("—" if key!="exceed" else f"{int(b)}/{b_total}→{int(c)}/{c_total}"); continue
                change=(b-c)/abs(b)*100 if key in lower_better else (c-b)/abs(b)*100
                finite_changes.append(change)
                arrow="↓" if c<b else "↑" if c>b else "→"
                differences.append(f"{change:+.1f}% {arrow}")
            tag="better" if finite_changes and min(finite_changes)>=0 else "worse" if finite_changes and max(finite_changes)<0 else ""
            table.insert("","end",values=(category,label,*differences),tags=(tag,) if tag else ())
        self.autosize_metric_columns(table)

    @staticmethod
    def autosize_metric_columns(table):
        normal=tkfont.nametofont("TkDefaultFont"); bold=tkfont.Font(font=normal); bold.configure(weight="bold")
        for column in ("metric","X","Y","Z"):
            values=[table.heading(column,"text"),*(str(table.set(item,column)) for item in table.get_children())]
            width=max(normal.measure(value) for value in values)+24
            table.column(column,width=min(max(width,70),230),minwidth=70,stretch=False)
        table.column("category",width=100,minwidth=80,stretch=True)

    NO_STATUS_COLOR="#e5e7eb"

    @staticmethod
    def mask_color(value):
        return "#bfdbfe" if value==0 else "#1d4ed8"

    def set_status_offset(self,role,value):
        view=self.views[role]
        view["applied_offset"]=round(float(value),3); view["offset"].set(f"{view['applied_offset']:.3f}")
        if view["sensor"] is not None:self.run_analysis(role)

    def apply_status_offset_entry(self,role):
        view=self.views[role]
        try:value=float(view["offset"].get())
        except ValueError:
            view["offset"].set(f"{view['applied_offset']:.3f}"); messagebox.showerror(APP_NAME,"GPST 對時偏移必須是數字（秒）"); return
        if abs(value-view["applied_offset"])>=5e-4:self.set_status_offset(role,value)

    def on_mask_press(self,role,event):
        view=self.views[role]
        if (view["result"] is None or not view["mask_patches"] or event.button!=1
                or event.inaxes not in view["axes"] or event.xdata is None):return
        view["drag"]={"x":float(event.xdata),"delta":0.0,"drawn":0.0}

    def on_mask_drag(self,role,event):
        view=self.views[role]; drag=view["drag"]
        if drag is None or event.inaxes not in view["axes"] or event.xdata is None:return
        drag["delta"]=float(event.xdata)-drag["x"]
        view["offset"].set(f"{view['applied_offset']+drag['delta']:.3f}")
        now=time.perf_counter()
        if now-drag["drawn"]<.04:return  # redrawing the full waveform on every mouse event lags
        drag["drawn"]=now
        for patch,begin in view["mask_patches"]:patch.set_x(begin+drag["delta"])
        view["canvas"].draw_idle()

    def on_mask_release(self,role,event):
        view=self.views[role]; drag=view["drag"]; view["drag"]=None
        if drag is None:return
        if event.inaxes in view["axes"] and event.xdata is not None:drag["delta"]=float(event.xdata)-drag["x"]
        if abs(drag["delta"])<1e-3:
            view["offset"].set(f"{view['applied_offset']:.3f}"); return
        self.set_status_offset(role,view["applied_offset"]+drag["delta"])

    def render_diagnosis(self):
        if not hasattr(self,"diagnosis"):return
        event_table=self.diagnosis["events"]; evidence_table=self.diagnosis["evidence"]
        event_table.delete(*event_table.get_children()); evidence_table.delete(*evidence_table.get_children())
        candidate=self.views.get("candidate")
        if not candidate or candidate["result"] is None:
            self.diagnosis_status.set("請先在 Preview 匯入 Candidate CSV"); return
        try:
            selected_range=candidate["range"] or candidate["full_range"]
            result=diagnose_range(candidate["result"].aligned,candidate["result"].processed,*selected_range,
                                  float(self.target_hz.get()),float(self.threshold.get()),int(self.debounce_samples.get()))
            self.diagnosis_status.set(f"選定範圍 {selected_range[0]:.3f}–{selected_range[1]:.3f} s｜超標事件 {len(result.events)}｜{result.coverage}")
            for row in result.events.itertuples(index=False):
                def shown(value): return "—" if value is None or (isinstance(value,float) and np.isnan(value)) else value
                event_table.insert("","end",values=(f"{row.time_s:.3f}",row.axis,f"{row.peak_g:+.4f}",f"{row.duration_ms:.2f}",shown(row.mode),shown(row.motion_status),shown(row.selected_gripper),shown(row.commanded_position)))
            for source,evidence,strength,next_test in result.evidence:
                evidence_table.insert("","end",values=(source,evidence,strength,next_test))
            self.render_diagnostic_tool()
        except Exception as exc:
            self.diagnosis_status.set(f"診斷資料建立失敗：{exc}")

    def configure_diagnosis_table(self,specs):
        table=self.diagnosis["table"]; columns=tuple(column for column,_,_ in specs); table.configure(columns=columns)
        for column,label,width in specs:
            table.heading(column,text=label); table.column(column,width=width,anchor="center",stretch=column==columns[-1])

    def render_diagnostic_tool(self):
        if not hasattr(self,"diagnosis"):return
        figure=self.diagnosis["figure"]; canvas=self.diagnosis["canvas"]; table=self.diagnosis["table"]
        figure.clear(); table.delete(*table.get_children()); candidate=self.views.get("candidate")
        if not candidate or candidate["result"] is None:
            self.diagnosis_status.set("請先在 Preview 匯入 Candidate CSV"); canvas.draw_idle(); return
        try:
            selected_range=candidate["range"] or candidate["full_range"]; processed=candidate["result"].processed; hz=float(self.target_hz.get()); method=self.diagnosis_method.get()
            minimum=float(self.diagnosis_params["iso_min"].get()); maximum=float(self.diagnosis_params["iso_max"].get()); boundaries=tuple(float(self.diagnosis_params[key].get()) for key in ("iso_ab","iso_bc","iso_cd"))
            result=iso10816_reference(processed,*selected_range,hz,minimum,maximum,boundaries); plot=figure.subplots(1,1); values=result.velocity_rms_mm_s.to_numpy()
            baseline=self.views.get("baseline"); reference=None
            if baseline and baseline["result"] is not None:
                baseline_range=baseline["range"] or baseline["full_range"]
                reference=iso10816_reference(baseline["result"].processed,*baseline_range,hz,minimum,maximum,boundaries)
            positions=np.arange(3)
            if reference is None:
                plot.bar(positions,values,color="#315a7d",label="Candidate")
            else:
                width=.36
                plot.bar(positions-width/2,reference.velocity_rms_mm_s.to_numpy(),width,color="#9ca3af",label="Baseline")
                plot.bar(positions+width/2,values,width,color="#315a7d",label="Candidate")
            plot.set_xticks(positions,list("XYZ"))
            for boundary,label in zip(boundaries,("A/B","B/C","C/D")):plot.axhline(boundary,color="#6b7280",ls="--",lw=.8,label=f"{label} {boundary:g}")
            plot.set_ylabel("振動速度 RMS (mm/s)"); plot.legend(fontsize=7)
            meanings={"A":"低於 A/B","B":"介於 A/B 與 B/C","C":"介於 B/C 與 C/D","D":"高於 C/D"}
            if reference is None:
                self.configure_diagnosis_table((("axis","軸",55),("candidate","Candidate 振動速度 (mm/s)",190),("zone","參考區域",100),("meaning","區域說明",190)))
                for axis,row in result.iterrows():table.insert("","end",values=(axis,f"{row.velocity_rms_mm_s:.4f}",row.zone,meanings[row.zone]))
                comparison_text="尚未匯入 Baseline，目前只顯示 Candidate"
            else:
                self.configure_diagnosis_table((("axis","軸",55),("baseline","Baseline (mm/s)",140),("candidate","Candidate (mm/s)",140),("change","改善率",100),("zones","區域 Baseline→Candidate",190)))
                for axis,row in result.iterrows():
                    base=reference.loc[axis]; change=(base.velocity_rms_mm_s-row.velocity_rms_mm_s)/base.velocity_rms_mm_s*100 if base.velocity_rms_mm_s else np.nan
                    shown_change="—" if not np.isfinite(change) else f"{change:+.1f}%"
                    table.insert("","end",values=(axis,f"{base.velocity_rms_mm_s:.4f}",f"{row.velocity_rms_mm_s:.4f}",shown_change,f"{base.zone} → {row.zone}"))
                comparison_text="已與 Baseline 比較；改善率為正表示 Candidate 振動速度較低"
            self.diagnosis_status.set(f"ISO 10816 參考評估｜{minimum:g}–{maximum:g} Hz｜{comparison_text}｜不代表 WR503 驗收")
            canvas.draw_idle()
        except Exception as exc:
            self.diagnosis_status.set(f"{self.diagnosis_method.get()} 設定或計算錯誤：{exc}"); canvas.draw_idle()

    def render_tuning(self):
        if not hasattr(self,"tuning"):return
        candidate=self.views.get("candidate"); baseline=self.views.get("baseline")
        table=self.tuning["table"]; table.delete(*table.get_children()); figure=self.tuning["figure"]; figure.clear()
        if self.tuning.get("motion_cid") is not None:
            self.tuning["canvas"].mpl_disconnect(self.tuning["motion_cid"])
            self.tuning["motion_cid"]=None
        if not candidate or candidate["result"] is None:
            self.tuning_status.set("請先在 Preview 匯入 Candidate CSV"); self.tuning["canvas"].draw_idle(); return
        try:
            hz=float(self.target_hz.get()); cutoff=float(self.cutoff.get())
            c_range=candidate["range"] or candidate["full_range"]
            current=frequency_analysis(candidate["result"].processed,*c_range,hz,cutoff)
            reference=None
            if baseline and baseline["result"] is not None:
                b_range=baseline["range"] or baseline["full_range"]
                reference=frequency_analysis(baseline["result"].processed,*b_range,hz,cutoff)
            colors={"X":"#315a7d","Y":"#c45a3c","Z":"#4f7c59"}
            bands=((.5,10,"#dbeafe"),(10,50,"#fef3c7"),(50,cutoff,"#f3e8ff"))
            view_mode=self.tuning_view_mode.get()
            minimum=float(self.tuning_min_hz.get()); maximum=min(float(self.tuning_max_hz.get()),cutoff)
            peak_count=int(self.tuning_peak_count.get())
            if minimum<0 or maximum<=minimum:raise ValueError("頻率範圍設定無效")
            if peak_count<1:raise ValueError("峰值數必須大於 0")
            if view_mode=="STFT 時間—頻率":
                table.configure(columns=("kind","item","value"))
                for column,text in (("kind","分類"),("item","診斷項目"),("value","目前軸數值")):table.heading(column,text=text)
            else:
                table.configure(columns=("kind","item","X","Y","Z"))
                for column,text in (("kind","分類"),("item","診斷項目"),("X","X"),("Y","Y"),("Z","Z")):table.heading(column,text=text)
            if view_mode=="PSD 頻譜":
                axes=figure.subplots(3,1,sharex=True)
                for axis_name,plot in zip("XYZ",axes):
                    spectrum=current.spectra[axis_name]
                    for begin,end,color in bands:
                        if begin<end:plot.axvspan(begin,end,color=color,alpha=.38,zorder=0)
                    visible_spectrum=spectrum.frequency_hz.between(minimum,maximum)
                    plot.semilogy(spectrum.loc[visible_spectrum,"frequency_hz"],np.maximum(spectrum.loc[visible_spectrum,"psd_g2_hz"],1e-15),color=colors[axis_name],label="Candidate")
                    if reference:
                        base=reference.spectra[axis_name]
                        visible_base=base.frequency_hz.between(minimum,maximum)
                        plot.semilogy(base.loc[visible_base,"frequency_hz"],np.maximum(base.loc[visible_base,"psd_g2_hz"],1e-15),color="#6b7280",alpha=.8,label="Baseline")
                    peaks=current.peaks.query("axis==@axis_name and frequency_hz>=@minimum and frequency_hz<=@maximum").head(peak_count)
                    for peak in peaks.itertuples(index=False):
                        plot.scatter(peak.frequency_hz,max(peak.psd_g2_hz,1e-15),s=16,color=colors[axis_name],zorder=4)
                        plot.annotate(f"{peak.frequency_hz:.1f} Hz",(peak.frequency_hz,max(peak.psd_g2_hz,1e-15)),xytext=(3,4),textcoords="offset points",fontsize=6)
                    plot.set_ylabel(f"{axis_name} PSD\n(g²/Hz)"); plot.grid(alpha=.2); plot.legend(loc="upper right",fontsize=7)
                axes[-1].set_xlabel("頻率 (Hz)"); axes[-1].set_xlim(minimum,maximum)
            elif view_mode=="PSD 改善／惡化":
                axes=figure.subplots(3,1,sharex=True)
                for axis_name,plot in zip("XYZ",axes):
                    if reference is None:
                        plot.text(.5,.5,"需要匯入 Baseline 才能計算差異",ha="center",va="center",transform=plot.transAxes)
                        plot.set_axis_off(); continue
                    candidate_spectrum=current.spectra[axis_name]; base=reference.spectra[axis_name]
                    frequency=candidate_spectrum.frequency_hz.to_numpy(); c=np.maximum(candidate_spectrum.psd_g2_hz.to_numpy(),1e-15)
                    b=np.maximum(np.interp(frequency,base.frequency_hz,base.psd_g2_hz),1e-15)
                    delta=pd.Series(10*np.log10(c/b)).rolling(9,center=True,min_periods=1).median().to_numpy()
                    plot.axhline(0,color="#374151",lw=.8); plot.fill_between(frequency,0,delta,where=delta<=0,color="#16a34a",alpha=.65,label="改善")
                    plot.fill_between(frequency,0,delta,where=delta>0,color="#dc2626",alpha=.65,label="惡化")
                    limit=min(30,max(3,float(np.nanpercentile(np.abs(delta),98)))); plot.set_ylim(-limit,limit)
                    plot.set_ylabel(f"{axis_name} 差異"); plot.grid(alpha=.2); plot.legend(loc="upper right",fontsize=7)
                axes[-1].set_xlabel("頻率 (Hz)；0 以下為 Candidate 較低，0 以上為較高"); axes[-1].set_xlim(minimum,maximum)
            else:
                axis_name=self.spectrogram_axis.get(); plot=figure.subplots(1,1)
                window=float(self.tuning_window_s.get()); overlap=float(self.tuning_overlap_pct.get())
                stft_result=stft_analysis(candidate["result"].processed,*c_range,hz,axis_name,minimum,maximum,window,overlap)
                f_spec,t_spec,power=stft_result.frequency_hz,stft_result.time_s,stft_result.power_g2_hz
                power_db=10*np.log10(np.maximum(power,1e-15))
                mesh=plot.pcolormesh(t_spec,f_spec,power_db,shading="auto",cmap="viridis")
                colorbar=figure.colorbar(mesh,ax=plot); colorbar.set_label("震動能量（亮色較強）")
                aligned=candidate["result"].aligned
                visible=aligned.loc[aligned.time_s.between(*c_range)]
                if "motion_status" in visible:
                    moving=pd.to_numeric(visible.motion_status,errors="coerce").eq(0).to_numpy()
                    for index,(begin,end) in enumerate(self.runs(moving)):
                        if end>begin:plot.axvspan(visible.time_s.iloc[begin],visible.time_s.iloc[end-1],color="#60a5fa",alpha=.12,label="GMST 動作中" if index==0 else None)
                diagnosis=diagnose_range(aligned,candidate["result"].processed,*c_range,hz,float(self.threshold.get()),int(self.debounce_samples.get()))
                axis_events=diagnosis.events.loc[diagnosis.events.axis.eq(axis_name)] if not diagnosis.events.empty else diagnosis.events
                if not axis_events.empty:
                    event_times=axis_events.time_s.to_numpy(float)
                    plot.scatter(event_times,np.full(len(event_times),float(f_spec.max())),marker="v",s=16,
                                 color="#dc2626",alpha=.75,edgecolors="none",clip_on=True,label="超標事件")
                plot.set_ylabel(f"{axis_name} 頻率 (Hz)"); plot.set_xlabel("Sensor time (s)")
                if len(plot.get_legend_handles_labels()[0]):plot.legend(loc="upper right",fontsize=7)
                cursor_time=table.insert("","end",values=("時間定位","游標時間","將滑鼠移到圖上"))
                cursor_peak=table.insert("","end",values=("","最強頻率","—"))
                cursor_energy=table.insert("","end",values=("","能量","—"))
                cursor_state=table.insert("","end",values=("","RASeMIO 對時狀態","—"))
                table.insert("","end",values=("超標事件","目前軸事件數",str(len(axis_events))))
                for rank,event in enumerate(axis_events.head(3).itertuples(index=False),1):
                    table.insert("","end",values=("",f"事件 {rank}",f"{event.time_s:.3f} s｜{event.peak_g:+.3f} g"))

                def update_time_frequency_cursor(event):
                    if event.inaxes is not plot or event.xdata is None:return
                    time_index=int(np.abs(t_spec-float(event.xdata)).argmin())
                    frequency_index=int(np.nanargmax(power_db[:,time_index]))
                    cursor_s=float(t_spec[time_index]); peak_hz=float(f_spec[frequency_index]); energy_db=float(power_db[frequency_index,time_index])
                    nearest=aligned.iloc[(aligned.time_s-cursor_s).abs().to_numpy().argmin()]
                    def state_value(column,label):
                        value=nearest.get(column,np.nan)
                        return None if pd.isna(value) else f"{label}={value:g}" if isinstance(value,(int,float,np.integer,np.floating)) else f"{label}={value}"
                    state="｜".join(filter(None,(state_value("mode","GPST"),state_value("motion_status","GMST"),state_value("selected_gripper","GSID")))) or "無對時資料"
                    table.set(cursor_time,"value",f"{cursor_s:.3f} s")
                    table.set(cursor_peak,"value",f"{peak_hz:.2f} Hz")
                    table.set(cursor_energy,"value",f"{energy_db:.1f} dB")
                    table.set(cursor_state,"value",state)
                self.tuning["motion_cid"]=self.tuning["canvas"].mpl_connect("motion_notify_event",update_time_frequency_cursor)
            if view_mode!="STFT 時間—頻率":
              for band in current.bands.band.unique():
                candidate_values=[]; baseline_values=[]; ratio_values=[]
                for axis_name in "XYZ":
                    c=float(current.bands.query("axis==@axis_name and band==@band").power_g2.iloc[0])
                    candidate_values.append(f"{c:.2e} g²")
                    if reference is None:
                        baseline_values.append("—"); ratio_values.append("—")
                    else:
                        b=float(reference.bands.query("axis==@axis_name and band==@band").power_g2.iloc[0])
                        baseline_values.append(f"{b:.2e} g²")
                        ratio_values.append("—" if b==0 else f"{(b-c)/abs(b)*100:+.1f}%")
                category="頻帶能量" if band==current.bands.band.iloc[0] else ""
                table.insert("","end",values=(category,f"{band}－比例",*ratio_values),tags=("percentage",))
                table.insert("","end",values=("",f"{band}－基準值",*baseline_values))
                table.insert("","end",values=("",f"{band}－候選值",*candidate_values))
              for rank in range(1,peak_count+1):
                values=[]
                for axis_name in "XYZ":
                    match=current.peaks.query("axis==@axis_name and rank==@rank")
                    if match.empty:
                        values.append("—")
                    elif reference is None:
                        values.append(f"{float(match.frequency_hz.iloc[0]):.2f} Hz")
                    else:
                        base_match=reference.peaks.query("axis==@axis_name and rank==@rank")
                        values.append(
                            f"{float(base_match.frequency_hz.iloc[0]):.2f}→{float(match.frequency_hz.iloc[0]):.2f} Hz"
                            if not base_match.empty else f"—→{float(match.frequency_hz.iloc[0]):.2f} Hz"
                        )
                table.insert("","end",values=("主要峰值" if rank==1 else "",f"Top {rank}",*values))
            coverage_ok=(not candidate["result"].has_overlap or candidate["result"].covered_samples>0)
            comparison=(f"目前顯示 {self.spectrogram_axis.get()} 軸 STFT；滑鼠位置只更新此軸資料" if view_mode=="STFT 時間—頻率"
                        else "已與 Baseline 比較；正值代表 Candidate 頻帶能量降低" if reference
                        else "目前只有 Candidate，顯示絕對 PSD 與頻率線索")
            summary=comparison
            if reference is not None and view_mode!="STFT 時間—頻率":
                changes=[]
                for row in current.bands.itertuples(index=False):
                    base=reference.bands.query("axis==@row.axis and band==@row.band")
                    if not base.empty and float(base.power_g2.iloc[0])>0:
                        changes.append(((float(base.power_g2.iloc[0])-float(row.power_g2))/float(base.power_g2.iloc[0])*100,row.axis,row.band))
                if changes:
                    best=max(changes); worst=min(changes)
                    summary+=f"。最大改善：{best[1]} 軸{best[2]} {best[0]:+.1f}%；最大惡化：{worst[1]} 軸{worst[2]} {worst[0]:+.1f}%"
            dominant=[(float(stft_result.hotspots.iloc[0].frequency_hz),self.spectrogram_axis.get())] if view_mode=="STFT 時間—頻率" and not stft_result.hotspots.empty else []
            if view_mode!="STFT 時間—頻率":
                for axis_name in "XYZ":
                    peaks=current.peaks.query("axis==@axis_name and frequency_hz>=@minimum and frequency_hz<=@maximum")
                    if not peaks.empty:dominant.append((float(peaks.iloc[0].frequency_hz),axis_name))
            if dominant:
                frequency,axis_name=max(dominant)
                if frequency<10:next_step="主峰在低頻；先對照起停、速度與路徑，做降低加速度／jerk 的單變因重測"
                elif frequency<50:next_step="主峰在中頻；先確認是否只在特定姿態或動作出現，再比較路徑與驅動設定"
                else:next_step="存在中高頻峰；先用相同條件重複確認頻率穩定，再評估伺服抑振或 notch，勿直接套用一次量測"
                summary+=f"。調參線索：{axis_name} 軸 {frequency:.2f} Hz；{next_step}"
            self.tuning_status.set(f"{summary}。資料覆蓋：{'可分析' if coverage_ok else '不足'}；此頁提供驗證順序，不直接宣告根因。")
            for column in table["columns"]:
                values=[table.heading(column,"text"),*(str(table.set(item,column)) for item in table.get_children())]
                table.column(column,width=min(max(tkfont.nametofont("TkDefaultFont").measure(v) for v in values)+20,245),minwidth=70,stretch=column=="kind")
            self.tuning["canvas"].draw_idle()
        except ValueError as exc:
            self.tuning_status.set(str(exc)); self.tuning["canvas"].draw_idle()

    def on_plot_scroll(self,role,event):
        view=self.views[role]
        if view["result"] is None or event.inaxes not in view["axes"] or event.xdata is None:return
        left,right=view["axes"][-1].get_xlim(); factor=.8 if event.button=="up" else 1.25
        new_left=event.xdata-(event.xdata-left)*factor; new_right=event.xdata+(right-event.xdata)*factor
        full_left,full_right=view["full_range"]; width=min(new_right-new_left,full_right-full_left)
        new_left=max(full_left,min(new_left,full_right-width)); view["axes"][-1].set_xlim(new_left,new_left+width)
        view["xlim"]=(new_left,new_left+width)  # kept across re-analysis so alignment can be done zoomed in
        self.update_shared_y_limits(role); view["canvas"].draw_idle()

    def update_shared_y_limits(self,role):
        view=self.views[role]
        if view["result"] is None:return
        left,right=view["axes"][-1].get_xlim(); visible=view["result"].aligned.loc[view["result"].aligned.time_s.between(left,right),["X","Y","Z"]]
        if visible.empty:return
        threshold=float(self.threshold.get()); low=min(float(visible.min().min()),-threshold); high=max(float(visible.max().max()),threshold)
        padding=max((high-low)*.05,.01)
        for axis in view["axes"]:axis.set_ylim(low-padding,high+padding)
    def close_app(self): self.stop_event.set(); self.save_config(); self.store.close(); self.destroy()


def main():
    try:
        app = App()
    except (sqlite3.Error, OSError) as error:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(
            APP_NAME,
            f"無法建立或開啟觀測資料庫。\n\n原因：{error}\n\n"
            f"已嘗試的位置：\n  {APP_DIR}\n  {fallback_data_dir()}\n\n"
            "請把程式複製到本機磁碟的可寫資料夾再執行，例如桌面或文件資料夾。",
        )
        root.destroy()
        return
    app.mainloop()


if __name__=="__main__":main()
