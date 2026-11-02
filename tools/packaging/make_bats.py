# -*- coding: utf-8 -*-
"""生成三个启动脚本（GBK 编码）。

为什么是 GBK：cmd 读取 .bat 是按当前代码页逐字节解析的，
UTF-8 批处理里夹中文再 chcp 会导致后续行被按错误偏移切开
（表现为 "ingTrip.exe is not recognized" 这类诡异报错）。
用 GBK（中文 Windows 的默认代码页）且不 chcp，解析稳定、中文显示正常。
"""
from pathlib import Path

LAUNCH_EXE = r"""@echo off
setlocal
cd /d "%~dp0"
echo.
echo   正在启动 NanjingTrip Agent（免安装版）...
echo.
if not exist "exe\NanjingTrip\NanjingTrip.exe" goto missing
start "" "exe\NanjingTrip\NanjingTrip.exe" %*
exit /b 0
:missing
echo [错误] 未找到 exe\NanjingTrip\NanjingTrip.exe
echo 请确认压缩包已完整解压；或改用「启动软件-源码版.bat」。
echo.
pause
exit /b 1
"""

LAUNCH_SRC = r"""@echo off
setlocal
cd /d "%~dp0code"
echo ================================================
echo   NanjingTrip Agent  —  源码版启动
echo ================================================
where python >nul 2>nul
if errorlevel 1 goto nopython
python -c "import requests" >nul 2>nul
if errorlevel 1 goto installdeps
goto run
:installdeps
echo.
echo 首次运行，正在安装依赖（requests / pytest；已不再需要 Qt）...
python -m pip install -r requirements.txt
if errorlevel 1 goto pipfail
:run
echo.
echo 启动中...浏览器会自动打开 http://127.0.0.1:8765
echo 手机可连同一局域网，用控制台打印的地址访问
python main.py --demo
if errorlevel 1 goto runfail
exit /b 0
:nopython
echo.
echo [错误] 未检测到 Python。
echo 请安装 Python 3.11 或更高版本，安装时务必勾选 "Add python.exe to PATH"。
echo 下载地址: https://www.python.org/downloads/
echo 或者直接用免安装版: 启动软件-免安装.bat
echo.
pause
exit /b 1
:pipfail
echo.
echo [错误] 依赖安装失败，请检查网络后手动执行:
echo     pip install -r requirements.txt
echo.
pause
exit /b 1
:runfail
echo.
echo [提示] 程序异常退出。可用下面命令排查环境:
echo     python main.py --no-browser --port 8800
echo.
pause
exit /b 1
"""

RUN_TESTS = r"""@echo off
setlocal
cd /d "%~dp0code"
echo ================================================
echo   NanjingTrip Agent  —  自动化测试
echo ================================================
where python >nul 2>nul
if errorlevel 1 goto nopython
python -m pytest tests -q
echo.
echo 预期结果: 179 passed
echo （解析 34 + 集成 12 + 排程 10 + 助手 10 + 示意图 8 + 路径详情 8
echo   仓储 7 + 公交方案 7 + 多端同步 6 + 建议采纳 6 + 批量操作 5
echo   出行建议 5 + 逐段方式 5 + 冲突建议 4 + 城市包 13）
echo   （营业时间与住宿 20 + 在线降级 12 + 多端同步 10）
echo.
pause
exit /b 0
:nopython
echo [错误] 未检测到 Python，无法运行测试。请安装 Python 3.11+ 后重试。
pause
exit /b 1
"""

FILES = {
    '启动软件-免安装.bat': LAUNCH_EXE,
    '启动软件-源码版.bat': LAUNCH_SRC,
    '运行测试.bat': RUN_TESTS,
}


def write_all(target_dir: Path) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in FILES.items():
        p = target_dir / name
        # 统一 CRLF + GBK，确保 cmd 解析稳定
        p.write_bytes(body.replace('\n', '\r\n').encode('gbk'))
        written.append(name)
    return written


if __name__ == '__main__':
    out = Path(__file__).resolve().parent
    print('已写出:', write_all(out))
