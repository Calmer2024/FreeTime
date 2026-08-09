"""PyInstaller hook for FreeTime app module"""
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Collect all submodules of the app package
hiddenimports = collect_submodules('app')

# Collect all data files from the app package
datas = []
