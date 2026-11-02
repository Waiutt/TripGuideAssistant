# -*- coding: utf-8 -*-
"""pytest 配置：让测试可直接导入 nanjingtrip 包。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # code/
sys.path.insert(0, str(ROOT))
