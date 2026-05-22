#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JavBus 刮削器 + N_m3u8DL 下载器 GUI 界面 - 绿联 NAS 专用版
使用 tkinter 构建，无需额外安装
"""

import os
import re
import sys
import threading
import shlex
import zipfile
import subprocess
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional
from urllib.parse import urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET

from curl_cffi import requests
from PIL import Image
import json

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    print("错误：无法导入 tkinter。请确保 Python 安装了 tkinter 支持。")
    sys.exit(1)

# ===== 路径配置 =====
def get_base_dir():
    """获取程序基础目录，兼容打包和开发环境"""
    if getattr(sys, 'frozen', False):
        # 打包后运行
        return os.path.dirname(sys.executable)
    else:
        # 开发环境运行
        return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()

# ===== 配置文件管理 =====
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "downloader": {
        "save_dir": "",
        "use_proxy": True,
        "proxy": "http://127.0.0.1:10808"
    },
    "scraper": {
        "video_dir": "",
        "media_root": "",
        "use_proxy": True,
        "proxy": "http://127.0.0.1:10808/",
        "domain": "www.javbus.com",
        "timeout": 30,
        "domains": [
            "www.javbus.com",
            "www.busdmm.ink",
            "www.dmmsee.bond"
        ]
    }
}


def load_config():
    """加载配置文件"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并默认配置，确保所有键都存在
                merged_config = DEFAULT_CONFIG.copy()
                for key in merged_config:
                    if key in config:
                        merged_config[key].update(config[key])
                # 确保域名列表完整
                if 'domains' in merged_config['scraper']:
                    # 保留用户配置的域名，同时添加默认域名中缺失的
                    existing_domains = set(merged_config['scraper']['domains'])
                    for domain in DEFAULT_CONFIG['scraper']['domains']:
                        if domain not in existing_domains:
                            merged_config['scraper']['domains'].append(domain)
                return merged_config
        return DEFAULT_CONFIG.copy()
    except Exception as e:
        print(f"加载配置文件失败: {e}")
        return DEFAULT_CONFIG.copy()


def save_config(config):
    """保存配置文件"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存配置文件失败: {e}")
        return False


# 加载全局配置
CONFIG = load_config()
# 更新 SCRAPER_DOMAINS
SCRAPER_DOMAINS = CONFIG['scraper']['domains']


# ============ 工具配置 ============
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
N_m3u8DL_FILENAME = "N_m3u8DL-RE.exe"
FFMPEG_FILENAME = "ffmpeg.exe"

# GitHub API 端点
N_m3u8DL_API_URL = "https://api.github.com/repos/nilaoda/N_m3u8DL-RE/releases/latest"
FFMPEG_API_URL = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"


# ============ 配置 ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ============ 数据模型 ============
@dataclass
class AVMetadata:
    title: str = ""
    cover: str = ""
    avid: str = ""
    actress: dict = field(default_factory=dict)
    description: str = ""
    duration: str = ""
    release_date: str = ""
    year: str = ""
    keywords: list = field(default_factory=list)
    fanarts: list = field(default_factory=list)


# ============ 刮削器核心类 ============
class JavBusScraper:
    """绿联 NAS 专用刮削器"""
    
    def __init__(self, output_path: str = ".", base_dir: str = "", proxy: Optional[str] = None, 
                 timeout: int = 30, domain: Optional[str] = None,
                 progress_callback=None, log_callback=None):
        self.output_path = output_path
        self.base_dir = base_dir  # 绿联 NAS 媒体库根目录
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.timeout = timeout
        self.domain = domain if domain else self._get_random_domain()
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        
    def _get_random_domain(self) -> str:
        import random
        return random.choice(SCRAPER_DOMAINS)
    
    def _log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            self.log_callback(message, level)
        
    def _update_progress(self, current: int, total: int, message: str = ""):
        if self.progress_callback:
            self.progress_callback(current, total, message)
    
    def _to_relative_path(self, abs_path: str) -> str:
        """将绝对路径转换为相对于 base_dir 的路径"""
        if not self.base_dir:
            return abs_path
        try:
            # 标准化路径
            abs_path = os.path.normpath(abs_path)
            base_dir = os.path.normpath(self.base_dir)
            if abs_path.startswith(base_dir):
                return abs_path[len(base_dir):].lstrip(os.sep)
            return abs_path
        except:
            return abs_path
    
    def _fetch_html(self, url: str, referer: str = "") -> Optional[str]:
        try:
            headers = HEADERS.copy()
            if referer:
                headers["Referer"] = referer
            
            response = requests.get(
                url,
                proxies=self.proxy,
                headers=headers,
                timeout=self.timeout,
                impersonate="chrome110",
                allow_redirects=False
            )
            response.raise_for_status()
            return response.text
        except Exception as e:
            self._log(f"请求失败: {url}, 错误: {e}", "ERROR")
            return None
    
    def _download_file(self, url: str, filepath: str, referer: str = "") -> bool:
        try:
            headers = HEADERS.copy()
            if referer:
                headers["Referer"] = referer
            
            response = requests.get(
                url,
                stream=True,
                proxies=self.proxy,
                headers=headers,
                timeout=self.timeout,
                impersonate="chrome110",
                allow_redirects=False
            )
            response.raise_for_status()
            
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
        except Exception as e:
            self._log(f"下载失败: {url}, 错误: {e}", "ERROR")
            return False
    
    def _crop_poster(self, source_path: str, poster_path: str) -> bool:
        """从横版封面裁剪出竖版海报 (1080x1920 比例)"""
        try:
            with Image.open(source_path) as img:
                width, height = img.size
                # 计算竖版海报尺寸 (9:16 比例)
                target_ratio = 9 / 16
                crop_width = int(height * target_ratio)
                
                if crop_width > width:
                    # 如果图片太窄，从中间裁剪
                    crop_width = width
                    left = 0
                else:
                    # 从右侧裁剪 (通常右侧是人物主体)
                    left = width - crop_width
                
                # 裁剪并保存
                cropped = img.crop((left, 0, left + crop_width, height))
                
                # 调整尺寸为 1080x1920 (绿联推荐尺寸)
                poster = cropped.resize((1080, 1920), Image.Resampling.LANCZOS)
                poster.save(poster_path, "JPEG", quality=95)
                
                self._log(f"竖版海报已生成: {poster_path} (1080x1920)")
            return True
        except Exception as e:
            self._log(f"裁剪海报失败: {e}", "ERROR")
            # 如果裁剪失败，复制原图
            try:
                import shutil
                shutil.copy2(source_path, poster_path)
                self._log("已使用原图作为海报")
                return True
            except:
                return False
    
    def _extract_metadata(self, html: str) -> Optional[AVMetadata]:
        try:
            metadata = AVMetadata()
            
            # 1. 提取 avid (番号)
            avid_match = re.search(r'<title>((\d|[A-Z])+-\d+)', html)
            if not avid_match:
                self._log("无法提取番号", "ERROR")
                return None
            metadata.avid = avid_match.group(1)
            
            # 2. 提取标题
            title_match = re.search(r'<title>(.*?) - JavBus</title>', html)
            if not title_match:
                self._log("无法提取标题", "ERROR")
                return None
            metadata.title = title_match.group(1)
            
            # 3. 提取封面图
            cover_match = re.search(r'<a class="bigImage" href="([^"]+)"><img src="([^"]+)"', html)
            if not cover_match:
                self._log("无法提取封面", "ERROR")
                return None
            cover = cover_match.group(1)
            metadata.cover = cover if self._is_complete_url(cover) else f"https://{self.domain}{cover}"
            
            # 4. 提取描述
            desc_match = re.search(r'<meta name="description" content="([^"]+)">', html)
            metadata.description = desc_match.group(1) if desc_match else ""
            
            # 5. 提取关键字
            keywords_match = re.search(r'<meta name="keywords" content="([^"]+)">', html)
            if keywords_match:
                metadata.keywords = [k.strip() for k in keywords_match.group(1).split(',') if k.strip()]
            
            # 6. 提取发行日期
            date_match = re.search(r'<span class="header">發行日期:</span> ([^<]+)', html)
            date_str = date_match.group(1).strip() if date_match else ""
            metadata.release_date = date_str
            
            # 提取年份
            if date_str:
                year_match = re.search(r'(\d{4})', date_str)
                if year_match:
                    metadata.year = year_match.group(1)
            
            # 7. 提取时长
            duration_match = re.search(r'<span class="header">長度:</span> ([^<]+)', html)
            metadata.duration = duration_match.group(1).strip() if duration_match else ""
            
            # 8. 提取演员
            actors_pattern = r'<a class="avatar-box" href="[^"]+">\s*<div class="photo-frame">\s*<img src="([^"]+)"[^>]+>\s*</div>\s*<span>([^<]+)</span>'
            actresses = re.findall(actors_pattern, html)
            for img, name in actresses:
                img_url = img if self._is_complete_url(img) else f"https://{self.domain}{img}"
                metadata.actress[name] = img_url
            
            # 9. 提取样品图
            fanart_pattern = r'<a class="sample-box" href="(.*?\.jpg)">'
            metadata.fanarts = re.findall(fanart_pattern, html)
            
            self._log(f"成功提取元数据: {metadata.avid} - {metadata.title}")
            return metadata
            
        except Exception as e:
            self._log(f"解析 HTML 失败: {e}", "ERROR")
            return None
    
    def _is_complete_url(self, url: str) -> bool:
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except:
            return False
    
    def _generate_nfo(self, metadata: AVMetadata, folder_path: str) -> bool:
        """
        生成绿联 NAS 格式的 NFO 文件
        
        绿联规范：
        - title: 电影标题
        - year: 年份
        - plot: 简介
        - releasedate: 发行日期 (YYYY-MM-DD)
        - rating: 评分
        - genre: 风格 (使用 ID: 10749=爱情, 28=动作)
        - actor: 演员
        """
        try:
            root = ET.Element("movie")
            
            # 标题
            ET.SubElement(root, "title").text = metadata.title
            
            # 年份
            if metadata.year:
                ET.SubElement(root, "year").text = metadata.year
            
            # 简介
            if metadata.description:
                ET.SubElement(root, "plot").text = metadata.description
            
            # 发行日期
            if metadata.release_date:
                try:
                    # 尝试标准化日期格式
                    release_date = datetime.strptime(metadata.release_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                    ET.SubElement(root, "releasedate").text = release_date
                except ValueError:
                    ET.SubElement(root, "releasedate").text = metadata.release_date
            
            # 评分 (绿联没有从 JavBus 获取评分的字段，设为空或默认值)
            # JavBus 没有评分字段，这里留空
            
            # 风格固定为: 爱情(10749) 和 动作(28)
            ET.SubElement(root, "genre").text = "10749"  # 爱情
            ET.SubElement(root, "genre").text = "28"     # 动作
            
            # 演员 (绿联格式)
            for name in metadata.actress.keys():
                actor = ET.SubElement(root, "actor")
                ET.SubElement(actor, "name").text = name
                # 注意：绿联 NAS 不支持 thumb 字段，已经去掉
            
            # 写入文件
            xml_str = ET.tostring(root, encoding='utf-8')
            dom = minidom.parseString(xml_str)
            
            # NFO 文件名：番号.nfo
            nfo_path = os.path.join(folder_path, f"{metadata.avid}.nfo")
            with open(nfo_path, 'w', encoding='utf-8') as f:
                dom.writexml(f, indent="  ", addindent="  ", newl="\n", encoding='utf-8')
            
            self._log(f"NFO 文件已生成: {nfo_path}")
            return True
            
        except Exception as e:
            self._log(f"生成 NFO 失败: {e}", "ERROR")
            return False
    
    def _find_and_move_video(self, avid: str, folder_path: str) -> bool:
        """
        查找并移动视频文件到对应文件夹
        安全策略：
        1. 如果目标位置已有同名视频，跳过移动
        2. 如果源文件和目标在同一位置，跳过移动
        3. 只移动，不删除任何文件
        """
        video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts']
        
        # 首先检查目标文件夹是否已有视频文件
        existing_video = None
        for ext in video_extensions:
            target_video = os.path.join(folder_path, f"{avid}{ext}")
            if os.path.exists(target_video):
                existing_video = target_video
                break
        
        if existing_video:
            self._log(f"目标目录已存在视频: {os.path.basename(existing_video)}")
            return True
        
        # 在根目录查找视频文件（精确匹配）
        for ext in video_extensions:
            source_path = os.path.join(self.output_path, f"{avid}{ext}")
            if os.path.exists(source_path):
                # 检查源文件是否已经在目标目录
                if os.path.dirname(source_path) == folder_path:
                    self._log(f"视频文件已在目标目录: {avid}{ext}")
                    return True
                
                target_path = os.path.join(folder_path, f"{avid}{ext}")
                
                # 安全检查：确保不会覆盖
                if os.path.exists(target_path):
                    self._log(f"目标位置已存在文件，跳过移动: {avid}{ext}")
                    return True
                
                try:
                    os.rename(source_path, target_path)
                    self._log(f"视频文件已移动: {avid}{ext}")
                    return True
                except Exception as e:
                    self._log(f"移动视频文件失败: {e}", "ERROR")
                    return False
        
        # 如果没找到，尝试模糊匹配（只在根目录查找）
        try:
            root_files = [f for f in os.listdir(self.output_path) 
                         if os.path.isfile(os.path.join(self.output_path, f))]
            
            for filename in root_files:
                if avid.lower() in filename.lower():
                    for ext in video_extensions:
                        if filename.lower().endswith(ext):
                            source_path = os.path.join(self.output_path, filename)
                            target_path = os.path.join(folder_path, filename)
                            
                            # 安全检查
                            if os.path.exists(target_path):
                                self._log(f"目标位置已存在文件，跳过移动: {filename}")
                                return True
                            
                            try:
                                os.rename(source_path, target_path)
                                self._log(f"视频文件已移动: {filename}")
                                return True
                            except Exception as e:
                                self._log(f"移动视频文件失败: {e}", "ERROR")
                                return False
        except Exception as e:
            self._log(f"扫描视频文件失败: {e}", "WARNING")
        
        self._log(f"未找到可移动的视频文件: {avid}", "WARNING")
        return True  # 没找到视频不算失败
    
    def scrape(self, avid: str) -> bool:
        avid = avid.upper().strip()
        self._log(f"开始刮削: {avid}")
        self._update_progress(0, 6, "获取网页...")
        
        url = f"https://{self.domain}/{avid}"
        html = self._fetch_html(url)
        if not html:
            self._log(f"无法获取页面: {url}", "ERROR")
            return False
        
        self._update_progress(1, 6, "解析元数据...")
        metadata = self._extract_metadata(html)
        if not metadata:
            return False
        
        # 创建影片目录
        folder_path = os.path.join(self.output_path, metadata.avid)
        Path(folder_path).mkdir(parents=True, exist_ok=True)
        
        self._update_progress(2, 6, "下载封面...")
        referer = f"https://{self.domain}/{avid}"
        
        # 先下载横版封面到临时文件
        temp_cover_path = os.path.join(folder_path, f"{metadata.avid}-temp-cover.jpg")
        if not self._download_file(metadata.cover, temp_cover_path, referer):
            self._log("封面下载失败", "ERROR")
            return False
        
        self._update_progress(3, 6, "处理海报...")
        # 从横版封面裁剪出竖版海报 (poster)
        poster_path = os.path.join(folder_path, f"{metadata.avid}-poster.jpg")
        self._crop_poster(temp_cover_path, poster_path)
        
        # 横版封面作为 backdrop
        backdrop_path = os.path.join(folder_path, f"{metadata.avid}-backdrop.jpg")
        try:
            # 重命名临时文件为 backdrop
            os.rename(temp_cover_path, backdrop_path)
        except:
            # 如果重命名失败，复制一份
            import shutil
            shutil.copy2(temp_cover_path, backdrop_path)
            os.remove(temp_cover_path)
        
        self._update_progress(4, 6, "生成 NFO...")
        if not self._generate_nfo(metadata, folder_path):
            return False
        
        self._update_progress(5, 6, "整理视频文件...")
        self._find_and_move_video(metadata.avid, folder_path)
        
        self._update_progress(6, 6, "完成!")
        self._log(f"刮削完成: {metadata.avid}")
        self._log(f"输出目录: {folder_path}")
        return True


# ============ GUI 界面 ============
class ScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("JavBus 刮削器 - 绿联 NAS 专用版")
        self.root.geometry("750x845")  # 高度从 650 增加到 845 (1.3倍)
        self.root.minsize(650, 715)    # 最小高度也相应增加
        
        self.style = ttk.Style()
        self.style.configure('TButton', padding=5)
        self.style.configure('TLabel', padding=3)
        
        self.scraping_thread = None
        
        self._create_widgets()
    
    def _create_widgets(self):
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        # ===== 刮削设置 =====
        input_frame = ttk.LabelFrame(main_frame, text="刮削设置", padding="10")
        input_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        input_frame.columnconfigure(1, weight=1)
        
        # 输出目录 (影片存放目录)
        ttk.Label(input_frame, text="影片目录:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.output_var = tk.StringVar(value="")
        self.output_entry = ttk.Entry(input_frame, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 5))
        
        ttk.Button(input_frame, text="浏览...", command=self.browse_output).grid(row=0, column=2, padx=(5, 0))
        
        # 刷新按钮
        ttk.Button(input_frame, text="🔄", command=self.refresh_file_list, width=3).grid(row=0, column=3, padx=(3, 0))
        
        # 文件选择下拉框
        ttk.Label(input_frame, text="选择文件:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.file_var = tk.StringVar()
        self.file_combo = ttk.Combobox(input_frame, textvariable=self.file_var, width=40, state="readonly")
        self.file_combo.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        self.file_combo.bind('<<ComboboxSelected>>', self.on_file_selected)
        
        # 车牌号输入（可编辑）
        ttk.Label(input_frame, text="车牌号:").grid(row=2, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.avid_var = tk.StringVar()
        self.avid_entry = ttk.Entry(input_frame, textvariable=self.avid_var, width=30)
        self.avid_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        self.avid_entry.bind('<Return>', lambda e: self.start_scrape())
        
        # 刮削按钮
        self.scrape_btn = ttk.Button(input_frame, text="开始刮削", command=self.start_scrape)
        self.scrape_btn.grid(row=2, column=2, padx=(5, 0), pady=(10, 0))
        
        # Base Dir (绿联 NAS 媒体库根目录，用于生成相对路径)
        ttk.Label(input_frame, text="媒体库根目录:").grid(row=3, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.base_dir_var = tk.StringVar(value="/volume1/share/Videos/MissAV")
        self.base_dir_entry = ttk.Entry(input_frame, textvariable=self.base_dir_var)
        self.base_dir_entry.grid(row=3, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        
        ttk.Label(input_frame, text="(NAS 内部路径，用于计算相对路径)", 
                foreground="gray").grid(row=4, column=0, columnspan=3, sticky=tk.W, padx=(85, 0))
        
        # ===== 代理设置 =====
        proxy_frame = ttk.LabelFrame(main_frame, text="代理设置", padding="10")
        proxy_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        proxy_frame.columnconfigure(1, weight=1)
        
        self.use_proxy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(proxy_frame, text="使用代理", variable=self.use_proxy_var, 
                       command=self.toggle_proxy).grid(row=0, column=0, sticky=tk.W)
        
        ttk.Label(proxy_frame, text="代理地址:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.proxy_var = tk.StringVar(value="")
        self.proxy_entry = ttk.Entry(proxy_frame, textvariable=self.proxy_var)
        self.proxy_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(5, 0))
        
        # ===== 高级设置 =====
        advanced_frame = ttk.LabelFrame(main_frame, text="高级设置", padding="10")
        advanced_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        
        ttk.Label(advanced_frame, text="域名:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.domain_var = tk.StringVar(value="www.javbus.com")
        domain_combo = ttk.Combobox(advanced_frame, textvariable=self.domain_var, 
                                    values=SCRAPER_DOMAINS, width=25, state="readonly")
        domain_combo.grid(row=0, column=1, sticky=tk.W, padx=(0, 20))
        
        ttk.Label(advanced_frame, text="超时(秒):").grid(row=0, column=2, sticky=tk.W, padx=(0, 5))
        self.timeout_var = tk.IntVar(value=30)
        ttk.Spinbox(advanced_frame, from_=10, to=120, textvariable=self.timeout_var, width=8).grid(row=0, column=3, sticky=tk.W)
        
        # 风格固定提示
        style_frame = ttk.Frame(advanced_frame)
        style_frame.grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Label(style_frame, text="固定风格: ", font=('', 9, 'bold')).pack(side=tk.LEFT)
        ttk.Label(style_frame, text="爱情(10749) + 动作(28)", foreground="blue").pack(side=tk.LEFT)
        
        # ===== 进度条 =====
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, mode='determinate')
        self.progress_bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 10))
        
        self.progress_label = ttk.Label(progress_frame, text="就绪", width=15)
        self.progress_label.grid(row=0, column=1)
        
        # ===== 日志区域 =====
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 日志按钮
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(5, 0))
        
        ttk.Button(log_btn_frame, text="清空日志", command=self.clear_log).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(log_btn_frame, text="保存日志", command=self.save_log).pack(side=tk.LEFT)
        ttk.Button(log_btn_frame, text="打开输出目录", command=self.open_output_dir).pack(side=tk.RIGHT)
        
        # ===== 底部按钮 =====
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.grid(row=5, column=0, columnspan=3, sticky=(tk.W, tk.E))
        
        ttk.Button(bottom_frame, text="退出", command=self.root.quit).pack(side=tk.RIGHT)
        
        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.grid(row=6, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(10, 0))
        
        # 初始化时刷新文件列表
        self.root.after(500, self.refresh_file_list)
    
    def toggle_proxy(self):
        state = "normal" if self.use_proxy_var.get() else "disabled"
        self.proxy_entry.configure(state=state)
    
    def set_proxy(self, proxy_url: str):
        self.proxy_var.set(proxy_url)
        self.use_proxy_var.set(True)
        self.toggle_proxy()
    
    def browse_output(self):
        directory = filedialog.askdirectory(initialdir=self.output_var.get())
        if directory:
            self.output_var.set(directory)
            self.refresh_file_list()
    
    def _extract_avid_from_filename(self, filename: str) -> str:
        """从文件名提取车牌号"""
        # 移除扩展名
        name = os.path.splitext(filename)[0]
        
        # 匹配连续的数字、字母、减号的组合（从开头匹配）
        # 例如: ABC-123, ABC123, 123-ABC, A1B2-C3, 等等
        pattern = r'^[a-zA-Z0-9-]+'
        match = re.search(pattern, name)
        
        if match:
            # 返回第一个匹配到的连续组合，转大写
            return match.group(0).upper()
        
        # 如果没匹配到，返回文件名（大写，移除特殊字符）
        cleaned = re.sub(r'[^\w\s-]', '', name).strip()
        return cleaned.upper() if cleaned else name.upper()
    
    def refresh_file_list(self):
        """刷新文件列表下拉框"""
        output_path = self.output_var.get().strip()
        
        if not os.path.exists(output_path):
            self.file_combo['values'] = []
            return
        
        file_list = []
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts')
        
        try:
            # 获取子目录（已刮削的）
            for item in os.listdir(output_path):
                item_path = os.path.join(output_path, item)
                if os.path.isdir(item_path):
                    # 标记为 [已刮削] 目录
                    file_list.append(f"[已刮削] {item}")
            
            # 获取视频文件（未刮削的）
            for item in os.listdir(output_path):
                item_path = os.path.join(output_path, item)
                if os.path.isfile(item_path) and item.lower().endswith(video_extensions):
                    file_list.append(f"[未刮削] {item}")
            
            self.file_combo['values'] = file_list
            
            if file_list:
                self.log(f"找到 {len(file_list)} 个项目")
            else:
                self.log("目录为空")
                
        except Exception as e:
            self.log(f"刷新文件列表失败: {e}", "ERROR")
            self.file_combo['values'] = []
    
    def on_file_selected(self, event=None):
        """下拉框选择事件"""
        selected = self.file_var.get()
        if not selected:
            return
        
        # 提取文件名（去掉前缀标记和空格）
        # [已刮削] 或 [未刮削] 前缀长度是 6 个字符（包括空格）
        if selected.startswith('[已刮削] '):
            filename = selected[6:].strip()
        elif selected.startswith('[未刮削] '):
            filename = selected[6:].strip()
        else:
            filename = selected.strip()
        
        # 自动提取车牌号
        avid = self._extract_avid_from_filename(filename)
        self.avid_var.set(avid)
        
        self.log(f"已选择: {filename} -> 车牌号: {avid}")
    
    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, log_line)
        
        if level == "ERROR":
            self.log_text.tag_add("error", f"{self.log_text.index('end-2l')}", f"{self.log_text.index('end-2l lineend')}")
            self.log_text.tag_config("error", foreground="red")
        elif level == "WARNING":
            self.log_text.tag_add("warning", f"{self.log_text.index('end-2l')}", f"{self.log_text.index('end-2l lineend')}")
            self.log_text.tag_config("warning", foreground="orange")
        
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.status_var.set(message)
    
    def update_progress(self, current: int, total: int, message: str = ""):
        progress = (current / total) * 100 if total > 0 else 0
        self.progress_var.set(progress)
        if message:
            self.progress_label.configure(text=message)
    
    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)
    
    def save_log(self):
        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.log_text.get(1.0, tk.END))
            messagebox.showinfo("保存成功", f"日志已保存到:\n{filepath}")
    
    def open_output_dir(self):
        output_path = self.output_var.get()
        if os.path.exists(output_path):
            os.startfile(output_path)
        else:
            messagebox.showwarning("目录不存在", f"目录不存在:\n{output_path}")
    
    def start_scrape(self):
        if self.scraping_thread and self.scraping_thread.is_alive():
            messagebox.showwarning("正在刮削", "请等待当前刮削任务完成")
            return
        
        avid = self.avid_var.get().strip()
        if not avid:
            messagebox.showwarning("输入错误", "请输入车牌号")
            return
        
        output_path = self.output_var.get().strip()
        if not os.path.exists(output_path):
            try:
                os.makedirs(output_path)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建输出目录:\n{e}")
                return
        
        base_dir = self.base_dir_var.get().strip()
        
        proxy = self.proxy_var.get().strip() if self.use_proxy_var.get() else None
        domain = self.domain_var.get() if self.domain_var.get() != "自动选择" else None
        timeout = self.timeout_var.get()
        
        self.scrape_btn.configure(state=tk.DISABLED)
        self.progress_var.set(0)
        self.log(f"开始刮削: {avid}")
        self.log(f"输出目录: {output_path}")
        if base_dir:
            self.log(f"媒体库根目录: {base_dir}")
        
        def scrape_task():
            try:
                scraper = JavBusScraper(
                    output_path=output_path,
                    base_dir=base_dir,
                    proxy=proxy,
                    timeout=timeout,
                    domain=domain,
                    progress_callback=self.update_progress,
                    log_callback=self.log
                )
                
                success = scraper.scrape(avid)
                
                if success:
                    self.log(f"✅ 刮削完成! 输出目录: {output_path}")
                else:
                    self.log("❌ 刮削失败，请查看日志", "ERROR")
                    
            except Exception as e:
                self.log(f"刮削异常: {e}", "ERROR")
            finally:
                self.root.after(0, lambda: self.scrape_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.progress_label.configure(text="就绪"))
        
        self.scraping_thread = threading.Thread(target=scrape_task, daemon=True)
        self.scraping_thread.start()



# ============ 工具管理器 ============
class ToolManager:
    """管理 N_m3u8DL 和 FFmpeg 工具的下载和检测"""
    
    def __init__(self, log_callback=None, progress_callback=None):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        Path(TOOLS_DIR).mkdir(parents=True, exist_ok=True)
    
    def _log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            self.log_callback(message, level)
    
    def _update_progress(self, current: int, total: int, message: str = ""):
        if self.progress_callback:
            self.progress_callback(current, total, message)
    
    def check_tools(self) -> dict:
        """检查工具是否存在"""
        n_m3u8dl_path = os.path.join(TOOLS_DIR, N_m3u8DL_FILENAME)
        ffmpeg_path = os.path.join(TOOLS_DIR, FFMPEG_FILENAME)
        
        # 也检查系统 PATH
        if not os.path.exists(n_m3u8dl_path):
            n_m3u8dl_path = shutil.which(N_m3u8DL_FILENAME.replace(".exe", ""))
            if n_m3u8dl_path and not n_m3u8dl_path.endswith(".exe"):
                n_m3u8dl_path += ".exe"
        
        if not os.path.exists(ffmpeg_path):
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path and not ffmpeg_path.endswith(".exe"):
                ffmpeg_path += ".exe"
        
        return {
            "n_m3u8dl": n_m3u8dl_path if (n_m3u8dl_path and os.path.exists(n_m3u8dl_path)) else None,
            "ffmpeg": ffmpeg_path if (ffmpeg_path and os.path.exists(ffmpeg_path)) else None
        }
    
    def _download_file(self, url: str, dest_path: str) -> bool:
        """下载文件并显示进度"""
        try:
            self._log(f"正在下载: {url}")
            
            response = requests.get(url, stream=True, impersonate="chrome110")
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(dest_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = int((downloaded / total_size) * 100)
                            self._update_progress(progress, 100, f"下载中 {progress}%")
            
            self._log(f"下载完成: {dest_path}")
            return True
        except Exception as e:
            self._log(f"下载失败: {e}", "ERROR")
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False
    
    def _extract_zip(self, zip_path: str, extract_to: str) -> bool:
        """解压 zip 文件"""
        try:
            self._log(f"正在解压: {zip_path}")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_to)
            self._log(f"解压完成")
            return True
        except Exception as e:
            self._log(f"解压失败: {e}", "ERROR")
            return False
    
    def _find_exe_in_dir(self, dir_path: str, exe_name: str) -> Optional[str]:
        """在目录中查找可执行文件"""
        for root, _, files in os.walk(dir_path):
            for file in files:
                if file.lower() == exe_name.lower():
                    return os.path.join(root, file)
        return None
    
    def _get_n_m3u8dl_download_url(self) -> Optional[str]:
        """从 GitHub API 获取 N_m3u8DL 最新版本的 Windows x64 下载链接"""
        try:
            self._log("正在查询 N_m3u8DL 最新版本...")
            response = requests.get(N_m3u8DL_API_URL, impersonate="chrome110")
            response.raise_for_status()
            data = response.json()
            
            # 在 assets 中查找 Windows x64 版本
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if "win-x64" in name.lower() and name.endswith(".zip"):
                    download_url = asset.get("browser_download_url")
                    version = data.get("tag_name", "unknown")
                    self._log(f"找到最新版本: {version}")
                    return download_url
            
            self._log("未找到 Windows x64 版本", "ERROR")
            return None
        except Exception as e:
            self._log(f"查询失败: {e}", "ERROR")
            return None
    
    def _get_ffmpeg_download_url(self) -> Optional[str]:
        """从 GitHub API 获取 FFmpeg 最新版本的 Windows x64 下载链接"""
        try:
            self._log("正在查询 FFmpeg 最新版本...")
            response = requests.get(FFMPEG_API_URL, impersonate="chrome110")
            response.raise_for_status()
            data = response.json()
            
            # 在 assets 中查找 Windows x64 gpl 版本
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if "win64-gpl" in name.lower() and name.endswith(".zip"):
                    download_url = asset.get("browser_download_url")
                    self._log(f"找到 FFmpeg 最新版本")
                    return download_url
            
            self._log("未找到 FFmpeg Windows x64 gpl 版本", "ERROR")
            return None
        except Exception as e:
            self._log(f"查询失败: {e}", "ERROR")
            return None
    
    def download_n_m3u8dl(self) -> bool:
        """下载并安装 N_m3u8DL"""
        zip_path = os.path.join(TOOLS_DIR, "n_m3u8dl.zip")
        temp_extract_dir = os.path.join(TOOLS_DIR, "n_m3u8dl_temp")
        
        try:
            # 获取最新版本下载链接
            download_url = self._get_n_m3u8dl_download_url()
            if not download_url:
                self._log("无法获取下载链接", "ERROR")
                return False
            
            if not self._download_file(download_url, zip_path):
                return False
            
            if not self._extract_zip(zip_path, temp_extract_dir):
                return False
            
            # 查找并移动可执行文件
            exe_path = self._find_exe_in_dir(temp_extract_dir, N_m3u8DL_FILENAME)
            if exe_path:
                dest_path = os.path.join(TOOLS_DIR, N_m3u8DL_FILENAME)
                shutil.move(exe_path, dest_path)
                self._log(f"N_m3u8DL 已安装: {dest_path}")
                return True
            else:
                self._log("未找到 N_m3u8DL 可执行文件", "ERROR")
                return False
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            if os.path.exists(temp_extract_dir):
                shutil.rmtree(temp_extract_dir)
    
    def download_ffmpeg(self) -> bool:
        """下载并安装 FFmpeg"""
        zip_path = os.path.join(TOOLS_DIR, "ffmpeg.zip")
        temp_extract_dir = os.path.join(TOOLS_DIR, "ffmpeg_temp")
        
        try:
            # 获取最新版本下载链接
            download_url = self._get_ffmpeg_download_url()
            if not download_url:
                self._log("无法获取下载链接", "ERROR")
                return False
            
            if not self._download_file(download_url, zip_path):
                return False
            
            if not self._extract_zip(zip_path, temp_extract_dir):
                return False
            
            # 查找 bin 目录并复制所有文件
            bin_dir = None
            for root, dirs, files in os.walk(temp_extract_dir):
                if "bin" in dirs:
                    bin_dir = os.path.join(root, "bin")
                    break
            
            if bin_dir and os.path.exists(bin_dir):
                # 复制 bin 目录下的所有文件到 TOOLS_DIR
                for filename in os.listdir(bin_dir):
                    src_file = os.path.join(bin_dir, filename)
                    dest_file = os.path.join(TOOLS_DIR, filename)
                    if os.path.isfile(src_file):
                        shutil.copy2(src_file, dest_file)
                        self._log(f"已复制: {filename}")
                self._log(f"FFmpeg 已完整安装到: {TOOLS_DIR}")
                return True
            else:
                # 如果找不到 bin 目录，尝试直接找 ffmpeg.exe
                exe_path = self._find_exe_in_dir(temp_extract_dir, FFMPEG_FILENAME)
                if exe_path:
                    dest_path = os.path.join(TOOLS_DIR, FFMPEG_FILENAME)
                    shutil.move(exe_path, dest_path)
                    self._log(f"FFmpeg 已安装: {dest_path}")
                    return True
                else:
                    self._log("未找到 FFmpeg 可执行文件", "ERROR")
                    return False
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            if os.path.exists(temp_extract_dir):
                shutil.rmtree(temp_extract_dir)


# ============ N_m3u8DL 下载器 ============
class M3U8Downloader:
    """N_m3u8DL 下载管理器"""
    
    def __init__(self, log_callback=None, progress_callback=None):
        self.process = None
        self.download_thread = None
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self._is_running = False
        self.tool_manager = ToolManager()
        self._cache_records = []  # 缓存记录：(原始文件名, 保存目录, 文件大小, 下载时间)
        self._current_is_smb = False
        self._current_target_dir = None
        self._current_save_name = None
        self._current_proxy = ""
        self.complete_callback = None  # 下载完成回调
        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    
    def _log(self, message: str, level: str = "INFO"):
        if self.log_callback:
            self.log_callback(message, level)
    
    def _update_progress(self, progress: int):
        if self.progress_callback:
            self.progress_callback(progress)
    
    def _is_smb_path(self, path: str) -> bool:
        """检测路径是否为 SMB 网络共享"""
        return path.startswith("\\\\") or path.startswith("//")
    
    def _copy_to_smb(self, source_path: str, dest_path: str) -> bool:
        """复制文件到 SMB 路径"""
        try:
            self._log(f"正在上传到 SMB 共享...")
            shutil.copy2(source_path, dest_path)
            self._log(f"✅ 上传完成: {dest_path}")
            return True
        except Exception as e:
            self._log(f"上传失败: {e}", "ERROR")
            return False
    
    def parse_params(self, params_str: str) -> dict:
        """解析 N_m3u8DL 参数字符串"""
        # 移除反引号
        params_str = params_str.replace('`', '').strip()
        
        result = {
            "url": "",
            "save_dir": None,  # 使用 None 表示未提供
            "save_name": "",
            "referer": "",
            "other_args": []
        }
        
        try:
            args = shlex.split(params_str)
            i = 0
            while i < len(args):
                arg = args[i]
                
                if arg.startswith("http") and not result["url"]:
                    result["url"] = arg
                elif arg in ["--save-dir", "-sv"] and i + 1 < len(args):
                    # 保存目录不覆盖，留给用户手动设置
                    # result["save_dir"] = args[i + 1]
                    i += 1
                elif arg in ["--save-name", "-sn"] and i + 1 < len(args):
                    result["save_name"] = args[i + 1]
                    i += 1
                elif arg in ["--referer", "-H"] and i + 1 < len(args):
                    ref = args[i + 1]
                    if ref.startswith("Referer:"):
                        ref = ref[len("Referer:"):]
                    result["referer"] = ref
                    i += 1
                elif arg:
                    result["other_args"].append(arg)
                
                i += 1
        except Exception as e:
            self._log(f"参数解析失败: {e}", "WARNING")
        
        return result
    
    def build_command(self, url: str, save_dir: str, save_name: str, referer: str, proxy: str = "", other_args: list = None) -> list:
        """构建命令"""
        tools = self.tool_manager.check_tools()
        if not tools["n_m3u8dl"]:
            raise RuntimeError("N_m3u8DL 未找到，请先在「工具管理」中下载")
        
        cmd = [tools["n_m3u8dl"]]
        
        # 添加 FFmpeg 路径
        if tools["ffmpeg"]:
            cmd.extend(["--ffmpeg-binary-path", tools["ffmpeg"]])
        
        # 添加代理
        if proxy:
            cmd.extend(["--custom-proxy", proxy])
        
        # URL 参数
        cmd.append(url)
        
        # 临时目录: cache/{save_name}
        if save_name:
            tmp_dir = os.path.join(CACHE_DIR, save_name)
            os.makedirs(tmp_dir, exist_ok=True)
            cmd.extend(["--tmp-dir", tmp_dir])
        
        # 保存目录
        if save_dir:
            # 展开环境变量
            save_dir_expanded = os.path.expandvars(save_dir)
            cmd.extend(["--save-dir", save_dir_expanded])
        
        # 文件名
        if save_name:
            cmd.extend(["--save-name", save_name])
        
        # Referer
        if referer:
            cmd.extend(["-H", f"Referer:{referer}"])
        
        # 其他参数
        if other_args:
            cmd.extend(other_args)
        
        return cmd
    
    def start_download(self, params_str: str = None, url: str = "", save_dir: str = "", 
                      save_name: str = "", referer: str = "", proxy: str = "", other_args: list = None,
                      complete_callback=None):
        """启动下载"""
        if self._is_running:
            self._log("已有下载任务正在进行", "WARNING")
            return False
        
        try:
            # 解析参数（只在有参数字符串时解析）
            if params_str:
                params = self.parse_params(params_str)
                # 只在没有设置的情况下使用解析的值
                url = url or params["url"]
                save_name = save_name or params["save_name"]
                referer = referer or params["referer"]
                if other_args is None:
                    other_args = params["other_args"]
            
            # 如果 save_dir 为空但用户没有设置，返回错误
            if not save_dir:
                self._log("请设置保存目录", "ERROR")
                return False
            
            # 检测目标路径是否为 SMB
            is_smb = self._is_smb_path(save_dir)
            
            # 设置实际下载目标
            if is_smb:
                # SMB 路径：先下载到本地缓存
                actual_save_dir = CACHE_DIR
                self._log(f"检测到 SMB 路径，将先下载到本地缓存")
            else:
                # 本地路径：直接下载
                actual_save_dir = save_dir
            
            # 确保目标目录存在
            if actual_save_dir and not os.path.exists(actual_save_dir):
                os.makedirs(actual_save_dir, exist_ok=True)
            
            # 构建命令
            cmd = self.build_command(url, actual_save_dir, save_name, referer, proxy, other_args)
            
            # 打印最终命令行
            cmd_str = ' '.join(cmd)
            self._log(f"最终命令行: {cmd_str}")
            
            self._is_running = True
            self._current_is_smb = is_smb
            self._current_target_dir = save_dir
            self._current_save_name = save_name
            self._current_proxy = proxy
            self.complete_callback = complete_callback
            
            self.download_thread = threading.Thread(
                target=self._download_task,
                args=(cmd,),
                daemon=True
            )
            self.download_thread.start()
            return True
        except Exception as e:
            self._log(f"启动下载失败: {e}", "ERROR")
            return False
    
    def _download_task(self, cmd: list):
        """下载任务线程"""
        downloaded_files = []
        final_files = []
        
        try:
            self._log(f"执行命令: {' '.join(cmd)}")
            
            # 不使用 cwd 参数，避免路径问题
            startupinfo = None
            if os.name == 'nt':  # Windows
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,  # 行缓冲
                startupinfo=startupinfo
            )
            
            for line in iter(self.process.stdout.readline, ''):
                if not line:
                    break
                
                line = line.strip()
                if line:
                    # 尝试多种编码
                    try:
                        if isinstance(line, bytes):
                            # 尝试 GBK
                            decoded = line.decode('gbk', errors='replace')
                        else:
                            # 如果是字符串，尝试用 GBK 编码再 UTF-8 解码
                            decoded = line.encode('gbk', errors='replace').decode('utf-8', errors='replace')
                    except:
                        decoded = str(line)
                    
                    self._log(decoded)
                    self._parse_progress(decoded)
            
            returncode = self.process.wait()
            
            # 查找下载的文件
            save_dir = self._current_target_dir if self._current_is_smb else actual_save_dir
            scan_dir = CACHE_DIR if self._current_is_smb else actual_save_dir
            
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts')
            for file in os.listdir(scan_dir if os.path.exists(scan_dir) else CACHE_DIR):
                if file.lower().endswith(video_extensions):
                    file_path = os.path.join(scan_dir, file)
                    if os.path.exists(file_path):
                        downloaded_files.append(file_path)
            
            if returncode == 0:
                self._log("✅ 下载完成!")
                
                # 如果是 SMB 路径，上传文件
                if self._current_is_smb and downloaded_files:
                    self._log(f"检测到 SMB 目标路径，开始上传...")
                    
                    # 确保 SMB 目录存在
                    try:
                        os.makedirs(self._current_target_dir, exist_ok=True)
                    except Exception as e:
                        self._log(f"创建 SMB 目录失败: {e}", "ERROR")
                    
                    for src_file in downloaded_files:
                        filename = os.path.basename(src_file)
                        dest_file = os.path.join(self._current_target_dir, filename)
                        
                        if self._copy_to_smb(src_file, dest_file):
                            final_files.append(dest_file)
                            # 添加到缓存记录
                            file_size = os.path.getsize(src_file)
                            self._cache_records.append({
                                "filename": filename,
                                "source_path": src_file,
                                "target_path": dest_file,
                                "size": file_size,
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            })
                            self._log(f"已保存到缓存: {src_file}")
                
                if final_files:
                    self._log(f"✅ 所有文件已上传到: {self._current_target_dir}")
                else:
                    self._log("文件保留在本地缓存目录")
            else:
                self._log(f"❌ 下载失败，返回码: {returncode}", "ERROR")
                
        except Exception as e:
            self._log(f"下载异常: {e}", "ERROR")
        finally:
            self._is_running = False
            self.process = None
            self._update_progress(0)
            self._current_is_smb = False
            self._current_target_dir = None
            self._current_save_name = None
            self._current_proxy = ""
            
            # 调用完成回调
            if self.complete_callback:
                try:
                    self.complete_callback(True, "下载任务已结束")
                except:
                    pass
    
    def _parse_progress(self, line: str):
        """解析进度信息"""
        # 匹配百分比，例如 "50%"
        import re
        match = re.search(r'(\d+(?:\.\d+)?)%', line)
        if match:
            try:
                progress = float(match.group(1))
                self._update_progress(int(progress))
            except:
                pass
    
    def stop_download(self):
        """停止下载"""
        if self.process and self.process.poll() is None:
            self._log("正在停止下载...")
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                self._log("下载已停止")
            except Exception as e:
                self._log(f"停止下载失败: {e}", "ERROR")
    
    def is_running(self) -> bool:
        return self._is_running
    
    def get_cache_records(self) -> list:
        """获取缓存记录"""
        return self._cache_records
    
    def get_cache_dir(self) -> str:
        """获取缓存目录路径"""
        return CACHE_DIR
    
    def delete_cache_file(self, source_path: str) -> bool:
        """删除缓存文件"""
        try:
            if os.path.exists(source_path):
                os.remove(source_path)
                # 从记录中移除
                self._cache_records = [r for r in self._cache_records if r["source_path"] != source_path]
                return True
            return False
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"删除失败: {e}", "ERROR")
            return False
    
    def cleanup_temp_files(self) -> dict:
        """清理临时文件，返回清理结果"""
        result = {
            "files": [],
            "total_size": 0,
            "errors": []
        }
        
        # 需要清理的文件模式
        patterns = ['*.ts', '*.meta', '*.json', '*.temp', '*.tmp']
        
        # 清理目录列表
        dirs_to_clean = [CACHE_DIR, TOOLS_DIR]
        
        for dir_path in dirs_to_clean:
            if not os.path.exists(dir_path):
                continue
            
            try:
                for file in os.listdir(dir_path):
                    file_lower = file.lower()
                    # 检查是否是临时文件
                    should_delete = False
                    
                    for pattern in patterns:
                        if pattern.replace('*', '') in file_lower:
                            should_delete = True
                            break
                    
                    # 检查是否是 .ts 文件
                    if file_lower.endswith('.ts'):
                        should_delete = True
                    
                    if should_delete:
                        file_path = os.path.join(dir_path, file)
                        try:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            result["files"].append(file_path)
                            result["total_size"] += file_size
                        except Exception as e:
                            result["errors"].append(f"{file_path}: {e}")
            except Exception as e:
                result["errors"].append(f"扫描 {dir_path} 失败: {e}")
        
        return result


# ============ GUI 界面 ============
class MainGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MissAV 工具箱 - 绿联 NAS 专用版")
        self.root.geometry("800x900")
        self.root.minsize(700, 750)
        
        self.style = ttk.Style()
        self.style.configure('TButton', padding=5)
        self.style.configure('TLabel', padding=3)
        
        self.scraping_thread = None
        self.downloader = M3U8Downloader(
            log_callback=self.downloader_log,
            progress_callback=self.downloader_progress
        )
        self.tool_manager = ToolManager(
            log_callback=self.tool_log,
            progress_callback=self.tool_progress
        )
        
        self._create_widgets()
        self._load_config()
    
    def _create_widgets(self):
        # 创建标签页
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(expand=True, fill='both', padx=10, pady=10)
        
        # 标签页 1: 下载器
        self.downloader_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.downloader_frame, text="⬇️ 下载器")
        self._create_downloader_tab()
        
        # 标签页 2: 下载缓存
        self.cache_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.cache_frame, text="📦 下载缓存")
        self._create_cache_tab()
        
        # 标签页 3: 刮削器
        self.scraper_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.scraper_frame, text="🎬 刮削器")
        self._create_scraper_tab()
        
        # 标签页 4: 工具管理
        self.tools_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.tools_frame, text="🔧 工具管理")
        self._create_tools_tab()
        
        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side='bottom', fill='x', padx=10, pady=5)
        
        # 初始检查工具
        self.root.after(500, self.check_tools_status)
    
    def _create_scraper_tab(self):
        """创建刮削器标签页"""
        main_frame = ttk.Frame(self.scraper_frame, padding="10")
        main_frame.pack(expand=True, fill='both')
        main_frame.columnconfigure(1, weight=1)
        
        # ===== 刮削设置 =====
        input_frame = ttk.LabelFrame(main_frame, text="刮削设置", padding="10")
        input_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        input_frame.columnconfigure(1, weight=1)
        
        # 输出目录 (影片存放目录)
        ttk.Label(input_frame, text="影片目录:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.output_var = tk.StringVar(value="")
        self.output_entry = ttk.Entry(input_frame, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 5))
        
        ttk.Button(input_frame, text="浏览...", command=self.browse_output).grid(row=0, column=2, padx=(5, 0))
        
        # 刷新按钮
        ttk.Button(input_frame, text="🔄", command=self.refresh_file_list, width=3).grid(row=0, column=3, padx=(3, 0))
        
        # 文件选择下拉框
        ttk.Label(input_frame, text="选择文件:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.file_var = tk.StringVar()
        self.file_combo = ttk.Combobox(input_frame, textvariable=self.file_var, width=40, state="readonly")
        self.file_combo.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        self.file_combo.bind('<<ComboboxSelected>>', self.on_file_selected)
        
        # 车牌号输入（可编辑）
        ttk.Label(input_frame, text="车牌号:").grid(row=2, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.avid_var = tk.StringVar()
        self.avid_entry = ttk.Entry(input_frame, textvariable=self.avid_var, width=30)
        self.avid_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        self.avid_entry.bind('<Return>', lambda e: self.start_scrape())
        
        # 刮削按钮
        self.scrape_btn = ttk.Button(input_frame, text="开始刮削", command=self.start_scrape)
        self.scrape_btn.grid(row=2, column=2, padx=(5, 0), pady=(10, 0))
        
        # Base Dir (绿联 NAS 媒体库根目录，用于生成相对路径)
        ttk.Label(input_frame, text="媒体库根目录:").grid(row=3, column=0, sticky=tk.W, padx=(0, 5), pady=(10, 0))
        self.base_dir_var = tk.StringVar(value="/volume1/share/Videos/MissAV")
        self.base_dir_entry = ttk.Entry(input_frame, textvariable=self.base_dir_var)
        self.base_dir_entry.grid(row=3, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=(0, 5), pady=(10, 0))
        
        ttk.Label(input_frame, text="(NAS 内部路径，用于计算相对路径)", 
                foreground="gray").grid(row=4, column=0, columnspan=3, sticky=tk.W, padx=(85, 0))
        
        # ===== 代理设置 =====
        proxy_frame = ttk.LabelFrame(main_frame, text="代理设置", padding="10")
        proxy_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        proxy_frame.columnconfigure(1, weight=1)
        
        self.use_proxy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(proxy_frame, text="使用代理", variable=self.use_proxy_var, 
                       command=self.toggle_proxy).grid(row=0, column=0, sticky=tk.W)
        
        ttk.Label(proxy_frame, text="代理地址:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.proxy_var = tk.StringVar(value="")
        self.proxy_entry = ttk.Entry(proxy_frame, textvariable=self.proxy_var)
        self.proxy_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(0, 5), pady=(5, 0))
        
        # ===== 高级设置 =====
        advanced_frame = ttk.LabelFrame(main_frame, text="高级设置", padding="10")
        advanced_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        
        ttk.Label(advanced_frame, text="域名:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.domain_var = tk.StringVar(value="www.javbus.com")
        domain_combo = ttk.Combobox(advanced_frame, textvariable=self.domain_var, 
                                    values=SCRAPER_DOMAINS, width=25, state="readonly")
        domain_combo.grid(row=0, column=1, sticky=tk.W, padx=(0, 20))
        
        ttk.Label(advanced_frame, text="超时(秒):").grid(row=0, column=2, sticky=tk.W, padx=(0, 5))
        self.timeout_var = tk.IntVar(value=30)
        ttk.Spinbox(advanced_frame, from_=10, to=120, textvariable=self.timeout_var, width=8).grid(row=0, column=3, sticky=tk.W)
        
        # 风格固定提示
        style_frame = ttk.Frame(advanced_frame)
        style_frame.grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Label(style_frame, text="固定风格: ", font=('', 9, 'bold')).pack(side=tk.LEFT)
        ttk.Label(style_frame, text="爱情(10749) + 动作(28)", foreground="blue").pack(side=tk.LEFT)
        
        # ===== 进度条 =====
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        
        self.scraper_progress_var = tk.DoubleVar(value=0)
        self.scraper_progress_bar = ttk.Progressbar(progress_frame, variable=self.scraper_progress_var, maximum=100, mode='determinate')
        self.scraper_progress_bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 10))
        
        self.scraper_progress_label = ttk.Label(progress_frame, text="就绪", width=15)
        self.scraper_progress_label.grid(row=0, column=1)
        
        # ===== 日志区域 =====
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)
        
        self.scraper_log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.scraper_log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 日志按钮
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(5, 0))
        
        ttk.Button(log_btn_frame, text="清空日志", command=self.clear_scraper_log).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(log_btn_frame, text="保存日志", command=self.save_scraper_log).pack(side=tk.LEFT)
        ttk.Button(log_btn_frame, text="打开输出目录", command=self.open_output_dir).pack(side=tk.RIGHT)
        
        # 初始化时刷新文件列表
        self.root.after(500, self.refresh_file_list)
    
    def _load_config(self):
        """从配置文件加载配置"""
        config = CONFIG
        # 加载下载器配置
        self.dl_dir_var.set(config['downloader']['save_dir'])
        self.dl_use_proxy_var.set(config['downloader']['use_proxy'])
        self.dl_proxy_var.set(config['downloader']['proxy'])
        
        # 加载刮削器配置
        self.output_var.set(config['scraper']['video_dir'])
        self.base_dir_var.set(config['scraper']['media_root'])
        self.use_proxy_var.set(config['scraper']['use_proxy'])
        self.proxy_var.set(config['scraper']['proxy'])
        self.domain_var.set(config['scraper']['domain'])
        self.timeout_var.set(config['scraper']['timeout'])
        
        # 更新代理输入框状态
        self.toggle_proxy()
    
    def _save_config(self):
        """保存配置到文件"""
        config = CONFIG.copy()
        # 保存下载器配置
        config['downloader']['save_dir'] = self.dl_dir_var.get()
        config['downloader']['use_proxy'] = self.dl_use_proxy_var.get()
        config['downloader']['proxy'] = self.dl_proxy_var.get()
        
        # 保存刮削器配置（注意：不保存域名列表，因为它是固定的）
        config['scraper']['video_dir'] = self.output_var.get()
        config['scraper']['media_root'] = self.base_dir_var.get()
        config['scraper']['use_proxy'] = self.use_proxy_var.get()
        config['scraper']['proxy'] = self.proxy_var.get()
        config['scraper']['domain'] = self.domain_var.get()
        config['scraper']['timeout'] = self.timeout_var.get()
        
        save_config(config)
    
    def _create_downloader_tab(self):
        """创建下载器标签页"""
        main_frame = ttk.Frame(self.downloader_frame, padding="10")
        main_frame.pack(expand=True, fill='both')
        main_frame.columnconfigure(1, weight=1)
        
        # ===== 参数粘贴区 =====
        paste_frame = ttk.LabelFrame(main_frame, text="快速粘贴（直接粘贴浏览器插件（猫抓）获取的完整参数）", padding="10")
        paste_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        paste_frame.columnconfigure(0, weight=1)
        
        self.params_text = scrolledtext.ScrolledText(paste_frame, height=4, wrap=tk.WORD)
        self.params_text.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 5))
        
        btn_frame = ttk.Frame(paste_frame)
        btn_frame.grid(row=1, column=0, sticky=(tk.W, tk.E))
        
        ttk.Button(btn_frame, text="📋 解析参数", command=self.parse_params).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="🗑️ 清空", command=lambda: self.params_text.delete(1.0, tk.END)).pack(side=tk.LEFT)
        
        # ===== 详细参数区 =====
        detail_frame = ttk.LabelFrame(main_frame, text="详细参数", padding="10")
        detail_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        detail_frame.columnconfigure(1, weight=1)
        
        # URL
        ttk.Label(detail_frame, text="m3u8 URL:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5), pady=(0, 5))
        self.dl_url_var = tk.StringVar()
        self.dl_url_entry = ttk.Entry(detail_frame, textvariable=self.dl_url_var)
        self.dl_url_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), pady=(0, 5))
        
        # 保存目录
        ttk.Label(detail_frame, text="保存目录:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(0, 5))
        self.dl_dir_var = tk.StringVar(value="")
        dl_dir_frame = ttk.Frame(detail_frame)
        dl_dir_frame.grid(row=1, column=1, sticky=(tk.W, tk.E), pady=(0, 5))
        dl_dir_frame.columnconfigure(0, weight=1)
        self.dl_dir_entry = ttk.Entry(dl_dir_frame, textvariable=self.dl_dir_var)
        self.dl_dir_entry.grid(row=0, column=0, sticky=(tk.W, tk.E))
        ttk.Button(dl_dir_frame, text="浏览...", command=self.browse_dl_dir).grid(row=0, column=1, padx=(5, 0))
        
        # 文件名
        ttk.Label(detail_frame, text="文件名:").grid(row=2, column=0, sticky=tk.W, padx=(0, 5), pady=(0, 5))
        self.dl_name_var = tk.StringVar()
        self.dl_name_entry = ttk.Entry(detail_frame, textvariable=self.dl_name_var)
        self.dl_name_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), pady=(0, 5))
        
        # Referer
        ttk.Label(detail_frame, text="Referer:").grid(row=3, column=0, sticky=tk.W, padx=(0, 5), pady=(0, 5))
        self.dl_referer_var = tk.StringVar()
        self.dl_referer_entry = ttk.Entry(detail_frame, textvariable=self.dl_referer_var)
        self.dl_referer_entry.grid(row=3, column=1, sticky=(tk.W, tk.E), pady=(0, 5))
        
        # HTTP 代理
        proxy_frame = ttk.Frame(detail_frame)
        proxy_frame.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(5, 0))
        
        self.dl_use_proxy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(proxy_frame, text="使用HTTP代理", variable=self.dl_use_proxy_var).pack(side=tk.LEFT, padx=(0, 5))
        
        self.dl_proxy_var = tk.StringVar(value="")
        self.dl_proxy_entry = ttk.Entry(proxy_frame, textvariable=self.dl_proxy_var, width=30)
        self.dl_proxy_entry.pack(side=tk.LEFT, fill='x', expand=True)
        
        # 其他参数
        ttk.Label(detail_frame, text="其他参数:").grid(row=5, column=0, sticky=tk.W, padx=(0, 5), pady=(0, 5))
        self.dl_other_var = tk.StringVar()
        self.dl_other_entry = ttk.Entry(detail_frame, textvariable=self.dl_other_var)
        self.dl_other_entry.grid(row=5, column=1, sticky=(tk.W, tk.E), pady=(0, 5))
        
        # ===== 下载控制区 =====
        control_frame = ttk.LabelFrame(main_frame, text="下载控制", padding="10")
        control_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        
        self.dl_start_btn = ttk.Button(control_frame, text="▶️ 开始下载", command=self.start_download)
        self.dl_start_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.dl_stop_btn = ttk.Button(control_frame, text="⏹️ 停止", command=self.stop_download, state=tk.DISABLED)
        self.dl_stop_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        # 进度条
        self.dl_progress_var = tk.DoubleVar(value=0)
        self.dl_progress_bar = ttk.Progressbar(control_frame, variable=self.dl_progress_var, maximum=100, mode='determinate')
        self.dl_progress_bar.pack(side=tk.LEFT, fill='x', expand=True, padx=(10, 0))
        
        self.dl_progress_label = ttk.Label(control_frame, text="就绪", width=10)
        self.dl_progress_label.pack(side=tk.LEFT, padx=(10, 0))
        
        # ===== 日志区域 =====
        log_frame = ttk.LabelFrame(main_frame, text="下载日志", padding="10")
        log_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)
        
        self.dl_log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.dl_log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(5, 0))
        
        ttk.Button(log_btn_frame, text="清空日志", command=self.clear_dl_log).pack(side=tk.LEFT)
    
    def _create_tools_tab(self):
        """创建工具管理标签页"""
        main_frame = ttk.Frame(self.tools_frame, padding="10")
        main_frame.pack(expand=True, fill='both')
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)
        
        # ===== 工具状态 =====
        status_frame = ttk.LabelFrame(main_frame, text="工具状态", padding="10")
        status_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))
        
        # N_m3u8DL
        self.n_m3u8dl_status_var = tk.StringVar(value="检查中...")
        ttk.Label(status_frame, text="N_m3u8DL-RE:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.n_m3u8dl_status_label = ttk.Label(status_frame, textvariable=self.n_m3u8dl_status_var)
        self.n_m3u8dl_status_label.grid(row=0, column=1, sticky=tk.W)
        
        # FFmpeg
        self.ffmpeg_status_var = tk.StringVar(value="检查中...")
        ttk.Label(status_frame, text="FFmpeg:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.ffmpeg_status_label = ttk.Label(status_frame, textvariable=self.ffmpeg_status_var)
        self.ffmpeg_status_label.grid(row=1, column=1, sticky=tk.W, pady=(5, 0))
        
        # ===== 下载按钮 =====
        btn_frame = ttk.LabelFrame(main_frame, text="下载工具", padding="10")
        btn_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))
        
        self.dl_n_m3u8dl_btn = ttk.Button(btn_frame, text="📥 下载 N_m3u8DL-RE", command=self.download_n_m3u8dl)
        self.dl_n_m3u8dl_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.dl_ffmpeg_btn = ttk.Button(btn_frame, text="📥 下载 FFmpeg", command=self.download_ffmpeg)
        self.dl_ffmpeg_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.refresh_tools_btn = ttk.Button(btn_frame, text="🔄 刷新状态", command=self.check_tools_status)
        self.refresh_tools_btn.pack(side=tk.LEFT)
        
        # ===== 进度条 =====
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))
        
        self.tool_progress_var = tk.DoubleVar(value=0)
        self.tool_progress_bar = ttk.Progressbar(progress_frame, variable=self.tool_progress_var, maximum=100, mode='determinate')
        self.tool_progress_bar.pack(fill='x', expand=True)
        
        self.tool_progress_label = ttk.Label(progress_frame, text="就绪")
        self.tool_progress_label.pack()
        
        # ===== 日志区域 =====
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)
        
        self.tool_log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.tool_log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        ttk.Button(log_frame, text="清空日志", command=self.clear_tool_log).grid(row=1, column=0, sticky=tk.W, pady=(5, 0))
    
    # ===== 刮削器方法 =====
    def toggle_proxy(self):
        state = "normal" if self.use_proxy_var.get() else "disabled"
        self.proxy_entry.configure(state=state)
    
    def set_proxy(self, proxy_url: str):
        self.proxy_var.set(proxy_url)
        self.use_proxy_var.set(True)
        self.toggle_proxy()
    
    def browse_output(self):
        directory = filedialog.askdirectory(initialdir=self.output_var.get())
        if directory:
            self.output_var.set(directory)
            self.refresh_file_list()
    
    def _extract_avid_from_filename(self, filename: str) -> str:
        name = os.path.splitext(filename)[0]
        pattern = r'^[a-zA-Z0-9-]+'
        match = re.search(pattern, name)
        if match:
            return match.group(0).upper()
        cleaned = re.sub(r'[^\w\s-]', '', name).strip()
        return cleaned.upper() if cleaned else name.upper()
    
    def refresh_file_list(self):
        output_path = self.output_var.get().strip()
        if not os.path.exists(output_path):
            self.file_combo['values'] = []
            return
        
        file_list = []
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts')
        
        try:
            for item in os.listdir(output_path):
                item_path = os.path.join(output_path, item)
                if os.path.isdir(item_path):
                    file_list.append(f"[已刮削] {item}")
            
            for item in os.listdir(output_path):
                item_path = os.path.join(output_path, item)
                if os.path.isfile(item_path) and item.lower().endswith(video_extensions):
                    file_list.append(f"[未刮削] {item}")
            
            self.file_combo['values'] = file_list
        except Exception as e:
            self.scraper_log(f"刷新文件列表失败: {e}", "ERROR")
            self.file_combo['values'] = []
    
    def on_file_selected(self, event=None):
        selected = self.file_var.get()
        if not selected:
            return
        
        if selected.startswith('[已刮削] '):
            filename = selected[6:].strip()
        elif selected.startswith('[未刮削] '):
            filename = selected[6:].strip()
        else:
            filename = selected.strip()
        
        avid = self._extract_avid_from_filename(filename)
        self.avid_var.set(avid)
        self.scraper_log(f"已选择: {filename} -> 车牌号: {avid}")
    
    def scraper_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        
        self.scraper_log_text.configure(state=tk.NORMAL)
        self.scraper_log_text.insert(tk.END, log_line)
        
        if level == "ERROR":
            self.scraper_log_text.tag_add("error", f"{self.scraper_log_text.index('end-2l')}", f"{self.scraper_log_text.index('end-2l lineend')}")
            self.scraper_log_text.tag_config("error", foreground="red")
        elif level == "WARNING":
            self.scraper_log_text.tag_add("warning", f"{self.scraper_log_text.index('end-2l')}", f"{self.scraper_log_text.index('end-2l lineend')}")
            self.scraper_log_text.tag_config("warning", foreground="orange")
        
        self.scraper_log_text.see(tk.END)
        self.scraper_log_text.configure(state=tk.DISABLED)
        self.status_var.set(message)
    
    def update_scraper_progress(self, current: int, total: int, message: str = ""):
        progress = (current / total) * 100 if total > 0 else 0
        self.scraper_progress_var.set(progress)
        if message:
            self.scraper_progress_label.configure(text=message)
    
    def clear_scraper_log(self):
        self.scraper_log_text.configure(state=tk.NORMAL)
        self.scraper_log_text.delete(1.0, tk.END)
        self.scraper_log_text.configure(state=tk.DISABLED)
    
    def save_scraper_log(self):
        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.scraper_log_text.get(1.0, tk.END))
            messagebox.showinfo("保存成功", f"日志已保存到:\n{filepath}")
    
    def open_output_dir(self):
        output_path = self.output_var.get()
        if os.path.exists(output_path):
            os.startfile(output_path)
        else:
            messagebox.showwarning("目录不存在", f"目录不存在:\n{output_path}")
    
    def start_scrape(self):
        if self.scraping_thread and self.scraping_thread.is_alive():
            messagebox.showwarning("正在刮削", "请等待当前刮削任务完成")
            return
        
        avid = self.avid_var.get().strip()
        if not avid:
            messagebox.showwarning("输入错误", "请输入车牌号")
            return
        
        output_path = self.output_var.get().strip()
        if not os.path.exists(output_path):
            try:
                os.makedirs(output_path)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建输出目录:\n{e}")
                return
        
        base_dir = self.base_dir_var.get().strip()
        proxy = self.proxy_var.get().strip() if self.use_proxy_var.get() else None
        domain = self.domain_var.get() if self.domain_var.get() != "自动选择" else None
        timeout = self.timeout_var.get()
        
        # 保存配置
        self._save_config()
        
        self.scrape_btn.configure(state=tk.DISABLED)
        self.scraper_progress_var.set(0)
        self.scraper_log(f"开始刮削: {avid}")
        self.scraper_log(f"输出目录: {output_path}")
        if base_dir:
            self.scraper_log(f"媒体库根目录: {base_dir}")
        
        def scrape_task():
            try:
                scraper = JavBusScraper(
                    output_path=output_path,
                    base_dir=base_dir,
                    proxy=proxy,
                    timeout=timeout,
                    domain=domain,
                    progress_callback=self.update_scraper_progress,
                    log_callback=self.scraper_log
                )
                
                success = scraper.scrape(avid)
                
                if success:
                    self.scraper_log(f"✅ 刮削完成! 输出目录: {output_path}")
                else:
                    self.scraper_log("❌ 刮削失败，请查看日志", "ERROR")
                    
            except Exception as e:
                self.scraper_log(f"刮削异常: {e}", "ERROR")
            finally:
                self.root.after(0, lambda: self.scrape_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.scraper_progress_label.configure(text="就绪"))
        
        self.scraping_thread = threading.Thread(target=scrape_task, daemon=True)
        self.scraping_thread.start()
    
    # ===== 下载器方法 =====
    def browse_dl_dir(self):
        directory = filedialog.askdirectory(initialdir=self.dl_dir_var.get())
        if directory:
            self.dl_dir_var.set(directory)
    
    def parse_params(self):
        params_str = self.params_text.get(1.0, tk.END).strip()
        if not params_str:
            messagebox.showwarning("提示", "请先粘贴参数")
            return
        
        params = self.downloader.parse_params(params_str)
        
        self.dl_url_var.set(params["url"])
        # 只在参数提供时更新保存目录，不覆盖用户的设置
        if params["save_dir"]:
            self.dl_dir_var.set(params["save_dir"])
        self.dl_name_var.set(params["save_name"])
        self.dl_referer_var.set(params["referer"])
        self.dl_other_var.set(' '.join(params["other_args"]))
        
        self.downloader_log("参数解析完成")
    
    def downloader_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        
        self.dl_log_text.configure(state=tk.NORMAL)
        self.dl_log_text.insert(tk.END, log_line)
        
        if level == "ERROR":
            self.dl_log_text.tag_add("error", f"{self.dl_log_text.index('end-2l')}", f"{self.dl_log_text.index('end-2l lineend')}")
            self.dl_log_text.tag_config("error", foreground="red")
        elif level == "WARNING":
            self.dl_log_text.tag_add("warning", f"{self.dl_log_text.index('end-2l')}", f"{self.dl_log_text.index('end-2l lineend')}")
            self.dl_log_text.tag_config("warning", foreground="orange")
        
        self.dl_log_text.see(tk.END)
        self.dl_log_text.configure(state=tk.DISABLED)
        self.status_var.set(message)
    
    def downloader_progress(self, progress: int):
        self.dl_progress_var.set(progress)
        self.dl_progress_label.configure(text=f"{progress}%")
    
    def clear_dl_log(self):
        self.dl_log_text.configure(state=tk.NORMAL)
        self.dl_log_text.delete(1.0, tk.END)
        self.dl_log_text.configure(state=tk.DISABLED)
    
    def start_download(self):
        # 保存配置
        self._save_config()
        
        # 检查工具
        tools = self.tool_manager.check_tools()
        if not tools["n_m3u8dl"] or not tools["ffmpeg"]:
            messagebox.showerror("错误", "请先在「工具管理」中下载 N_m3u8DL-RE 和 FFmpeg")
            self.notebook.select(self.tools_frame)
            return
        
        params_str = self.params_text.get(1.0, tk.END).strip()
        url = self.dl_url_var.get().strip()
        save_dir = self.dl_dir_var.get().strip()
        save_name = self.dl_name_var.get().strip()
        referer = self.dl_referer_var.get().strip()
        use_proxy = self.dl_use_proxy_var.get()
        proxy = self.dl_proxy_var.get().strip() if use_proxy else ""
        other_args = shlex.split(self.dl_other_var.get().strip()) if self.dl_other_var.get().strip() else None
        
        if not url and not params_str:
            messagebox.showwarning("输入错误", "请输入 m3u8 URL 或粘贴完整参数")
            return
        
        # 确保保存目录存在
        if save_dir and not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
        
        self.dl_start_btn.configure(state=tk.DISABLED)
        self.dl_stop_btn.configure(state=tk.NORMAL)
        self.dl_progress_var.set(0)
        
        def on_complete(success, message):
            self.root.after(0, lambda: self._reset_download_buttons())
        
        success = self.downloader.start_download(
            params_str=params_str,
            url=url,
            save_dir=save_dir,
            save_name=save_name,
            referer=referer,
            proxy=proxy,
            other_args=other_args,
            complete_callback=on_complete
        )
        
        if not success:
            self.dl_start_btn.configure(state=tk.NORMAL)
            self.dl_stop_btn.configure(state=tk.DISABLED)
    
    def stop_download(self):
        self.downloader.stop_download()
    
    def _reset_download_buttons(self):
        """重置下载按钮状态"""
        self.dl_start_btn.configure(state=tk.NORMAL)
        self.dl_stop_btn.configure(state=tk.DISABLED)
        self.refresh_cache_list()
    
    def check_tools_status(self):
        tools = self.tool_manager.check_tools()
        
        if tools["n_m3u8dl"]:
            self.n_m3u8dl_status_var.set(f"✅ 已安装 - {tools['n_m3u8dl']}")
            self.n_m3u8dl_status_label.configure(foreground="green")
        else:
            self.n_m3u8dl_status_var.set("❌ 未安装")
            self.n_m3u8dl_status_label.configure(foreground="red")
        
        if tools["ffmpeg"]:
            self.ffmpeg_status_var.set(f"✅ 已安装 - {tools['ffmpeg']}")
            self.ffmpeg_status_label.configure(foreground="green")
        else:
            self.ffmpeg_status_var.set("❌ 未安装")
            self.ffmpeg_status_label.configure(foreground="red")
    
    def download_n_m3u8dl(self):
        self.dl_n_m3u8dl_btn.configure(state=tk.DISABLED)
        self.dl_ffmpeg_btn.configure(state=tk.DISABLED)
        self.refresh_tools_btn.configure(state=tk.DISABLED)
        
        def task():
            try:
                success = self.tool_manager.download_n_m3u8dl()
                if success:
                    self.root.after(0, self.check_tools_status)
            finally:
                self.root.after(0, lambda: self.dl_n_m3u8dl_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.dl_ffmpeg_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.refresh_tools_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.tool_progress_var.set(0))
                self.root.after(0, lambda: self.tool_progress_label.configure(text="就绪"))
        
        threading.Thread(target=task, daemon=True).start()
    
    def download_ffmpeg(self):
        self.dl_n_m3u8dl_btn.configure(state=tk.DISABLED)
        self.dl_ffmpeg_btn.configure(state=tk.DISABLED)
        self.refresh_tools_btn.configure(state=tk.DISABLED)
        
        def task():
            try:
                success = self.tool_manager.download_ffmpeg()
                if success:
                    self.root.after(0, self.check_tools_status)
            finally:
                self.root.after(0, lambda: self.dl_n_m3u8dl_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.dl_ffmpeg_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.refresh_tools_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.tool_progress_var.set(0))
                self.root.after(0, lambda: self.tool_progress_label.configure(text="就绪"))
        
        threading.Thread(target=task, daemon=True).start()
    
    def tool_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        
        self.tool_log_text.configure(state=tk.NORMAL)
        self.tool_log_text.insert(tk.END, log_line)
        
        if level == "ERROR":
            self.tool_log_text.tag_add("error", f"{self.tool_log_text.index('end-2l')}", f"{self.tool_log_text.index('end-2l lineend')}")
            self.tool_log_text.tag_config("error", foreground="red")
        elif level == "WARNING":
            self.tool_log_text.tag_add("warning", f"{self.tool_log_text.index('end-2l')}", f"{self.tool_log_text.index('end-2l lineend')}")
            self.tool_log_text.tag_config("warning", foreground="orange")
        
        self.tool_log_text.see(tk.END)
        self.tool_log_text.configure(state=tk.DISABLED)
        self.status_var.set(message)
    
    def tool_progress(self, progress: int, total: int = 100, message: str = ""):
        self.tool_progress_var.set(progress)
        if message:
            self.tool_progress_label.configure(text=message)
    
    def clear_tool_log(self):
        self.tool_log_text.configure(state=tk.NORMAL)
        self.tool_log_text.delete(1.0, tk.END)
        self.tool_log_text.configure(state=tk.DISABLED)
    
    def _create_cache_tab(self):
        """创建下载缓存标签页"""
        main_frame = ttk.Frame(self.cache_frame, padding="10")
        main_frame.pack(expand=True, fill='both')
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # ===== 操作按钮 =====
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        
        ttk.Button(btn_frame, text="🔄 刷新", command=self.refresh_cache_list).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="🧹 全部清空", command=self.clear_all_cache).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="📂 打开缓存目录", command=self.open_cache_dir).pack(side=tk.LEFT)
        
        # ===== 目录树 =====
        tree_frame = ttk.LabelFrame(main_frame, text="缓存目录", padding="10")
        tree_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        
        # 创建 Treeview 作为目录树
        self.cache_tree = ttk.Treeview(tree_frame, show='tree', height=15)
        
        # 滚动条
        scrollbar = ttk.Scrollbar(tree_frame, orient='vertical', command=self.cache_tree.yview)
        self.cache_tree.configure(yscrollcommand=scrollbar.set)
        
        self.cache_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        
        # 右键菜单
        self.cache_menu = tk.Menu(self.cache_tree, tearoff=0)
        self.cache_menu.add_command(label="删除", command=self.delete_selected_cache)
        
        # 绑定事件
        self.cache_tree.bind("<Button-3>", self.show_cache_menu)
        self.cache_tree.bind("<Double-1>", self.on_tree_double_click)
    
    def on_tree_double_click(self, event):
        """双击展开/折叠目录"""
        item = self.cache_tree.identify_row(event.y)
        if item:
            item_path = self.cache_tree.item(item)['values'][0]
            if os.path.isdir(item_path):
                # 检查是否有占位符
                children = self.cache_tree.get_children(item)
                if children and self.cache_tree.item(children[0], 'text') == '':
                    # 加载子项
                    self.cache_tree.delete(children[0])
                    self._load_children(item, item_path)
                # 切换展开/折叠状态
                is_open = self.cache_tree.item(item, 'open')
                self.cache_tree.item(item, open=not is_open)
        
        # 初始加载
        self.refresh_cache_list()
    
    def refresh_cache_list(self):
        """刷新缓存列表"""
        # 清空现有项
        for item in self.cache_tree.get_children():
            self.cache_tree.delete(item)
        
        # 扫描缓存目录
        cache_dir = self.downloader.get_cache_dir()
        
        try:
            if os.path.exists(cache_dir):
                # 递归构建目录树
                self._build_tree('', cache_dir)
        except Exception as e:
            self.status_var.set(f"扫描缓存失败: {e}")
    
    def _build_tree(self, parent, path):
        """递归构建目录树"""
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            node_id = self.cache_tree.insert(parent, 'end', text=item, values=(item_path,))
            
            if os.path.isdir(item_path):
                self.cache_tree.insert(node_id, 'end')  # 添加占位符，稍后填充
                self.cache_tree.item(node_id, open=False)
            else:
                # 保存完整路径作为values
                pass
    
    def show_cache_menu(self, event):
        """显示右键菜单"""
        # 先选中右键点击的项
        item = self.cache_tree.identify_row(event.y)
        if item:
            self.cache_tree.selection_set(item)
            self.cache_tree.focus(item)
            # 检查是否是目录，如果是则展开/折叠
            try:
                # 检查是否是目录并需要加载子项
                item_path = self.cache_tree.item(item)['values'][0]
                if os.path.isdir(item_path):
                    # 检查是否有占位符，如果有则实际加载子项
                    children = self.cache_tree.get_children(item)
                    if children and self.cache_tree.item(children[0], 'text') == '':
                        # 移除占位符
                        self.cache_tree.delete(children[0])
                        # 加载实际子项
                        self._load_children(item, item_path)
            except:
                pass
            # 显示右键菜单
            self.cache_menu.post(event.x_root, event.y_root)
    
    def _load_children(self, parent, parent_path):
        """加载目录的子项"""
        for item in os.listdir(parent_path):
            item_path = os.path.join(parent_path, item)
            node_id = self.cache_tree.insert(parent, 'end', text=item, values=(item_path,))
            
            if os.path.isdir(item_path):
                self.cache_tree.insert(node_id, 'end')  # 添加占位符
                self.cache_tree.item(node_id, open=False)
    
    def clear_all_cache(self):
        """全部清空缓存"""
        if not messagebox.askyesno("确认", "确定要清空所有缓存文件和文件夹吗？"):
            return
        
        cache_dir = self.downloader.get_cache_dir()
        try:
            if os.path.exists(cache_dir):
                # 删除所有内容
                for item in os.listdir(cache_dir):
                    item_path = os.path.join(cache_dir, item)
                    try:
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    except Exception as e:
                        self.status_var.set(f"删除 {item} 失败: {e}")
            
            messagebox.showinfo("成功", "缓存已清空")
            self.refresh_cache_list()
        except Exception as e:
            messagebox.showerror("错误", f"清空缓存失败: {e}")
    
    def delete_selected_cache(self):
        """删除选中的缓存文件/文件夹"""
        selected = self.cache_tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要删除的文件或文件夹")
            return
        
        if not messagebox.askyesno("确认", "确定要删除选中的文件/文件夹吗？"):
            return
        
        deleted_count = 0
        for item in selected:
            item_path = self.cache_tree.item(item)['values'][0]
            
            try:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                    deleted_count += 1
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    deleted_count += 1
            except Exception as e:
                self.status_var.set(f"删除 {os.path.basename(item_path)} 失败: {e}")
        
        if deleted_count > 0:
            messagebox.showinfo("成功", f"已删除 {deleted_count} 个项目")
            self.refresh_cache_list()
    
    def open_cache_dir(self):
        """打开缓存目录"""
        cache_dir = self.downloader.get_cache_dir()
        if os.path.exists(cache_dir):
            os.startfile(cache_dir)
        else:
            messagebox.showwarning("提示", f"缓存目录不存在:\n{cache_dir}")


def main():
    root = tk.Tk()
    app = MainGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
