#!/usr/bin/env python3
"""
土耳其2023地震InSAR处理主脚本
基于 Türkiye_Earthquakes_2023.ipynb 参数配置
"""
import sys
import os
import shutil
from multiprocessing import freeze_support
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from insar_downloader import DownloadConfig, InSARDownloader
from insar_processor import InSARConfig, InSARProcessor


def main():
    """土耳其2023地震InSAR完整流程"""
    
    # 土耳其2023双震型地震
    # 震中1: 37.24°N, 38.11°E (Mw 7.8)
    # 震中2: 37.08°N, 37.17°E (Mw 7.5)
    data_dir = Path('/root/insar/data_turkey_2023')
    results_dir = Path('/root/insar/results/turkey')
    
    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 70)
    print("土耳其2023地震InSAR处理")
    print("=" * 70)
    print(f"数据目录: {data_dir}")
    print(f"结果目录: {results_dir}")
    print(f"地震1 (Mw 7.8): 37.24°N, 38.11°E")
    print(f"地震2 (Mw 7.5): 37.08°N, 37.17°E")
    
    # 下载配置 - 土耳其区域
    # 精确匹配notebook中使用的场景日期: 2023-01-29 和 2023-02-10
    # 区域: 覆盖双震震中 [35.5-39.0, 35.5-39.0]
    download_config = DownloadConfig(
        lon_min=35.5, lon_max=39.0,  # 覆盖震中区域
        lat_min=35.5, lat_max=39.0,  # 覆盖震中区域
        start_date='2023-01-29', end_date='2023-02-10',  # 精确匹配notebook日期
        data_dir=str(data_dir),
        asf_username='kanezeng',
        asf_password='#@!xiaoBOBO123'
    )
    
    # 验证/下载数据
    print("\n验证数据...")
    downloader = InSARDownloader(download_config)
    status = downloader.verify_data()
    
    if not status['ready_for_processing']:
        print("\n数据不完整，开始下载...")
        print("注意: 大范围区域需要更多下载时间")
        downloader.download_all()
        downloader.download_orbits()
    else:
        print("✓ 数据已就绪")
    
    # 再次验证
    status = downloader.verify_data()
    if not status['ready_for_processing']:
        print("✗ 数据准备失败")
        return 1
    
    # 处理配置 - 基于notebook参数
    processor_config = InSARConfig(
        data_dir=str(data_dir),
        work_dir=str(data_dir / 'work'),
        results_dir=str(results_dir),
        resolution=180,  # 180米分辨率（与notebook一致）
        multilook_wavelength=400,  # 400m多视波长
        multilook_coarsen=(12, 48),  # 12x48多视（与notebook一致）
        detrend_wavelength=300000,  # 300km去趋势波长（与notebook一致）
        earthquake_locations=[
            ('Turkey_Eq1_Mw78', 38.11, 37.24),  # Mw 7.8
            ('Turkey_Eq2_Mw75', 37.17, 37.08),  # Mw 7.5
        ]
    )
    
    # 运行处理
    print("\n运行InSAR处理...")
    print("步骤: 01.场景位置 -> 13.东西位移")
    processor = InSARProcessor(processor_config)
    
    try:
        processor.run_full_pipeline()
        
        print("\n" + "=" * 70)
        print("✓ 处理完成!")
        print("=" * 70)
        
        # 统计结果
        result_files = sorted(results_dir.glob('*.jpg'))
        print(f"\n生成结果: {len(result_files)} 张图像")
        for i, f in enumerate(result_files, 1):
            size_kb = f.stat().st_size / 1024
            print(f"  {i:2d}. {f.name} ({size_kb:.1f} KB)")
        
        # 复制到Windows
        win_dir = Path('/mnt/d/kimi-insar/results/turkey')
        win_dir.mkdir(parents=True, exist_ok=True)
        for f in result_files:
            shutil.copy2(f, win_dir / f.name)
        
        print(f"\n✓ 结果已复制到: {win_dir}")
        
    except Exception as e:
        print(f"\n✗ 处理失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == '__main__':
    freeze_support()
    sys.exit(main())
