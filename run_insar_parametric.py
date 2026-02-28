#!/usr/bin/env python3
"""
参数化InSAR处理主脚本
复用 insar_downloader 和 insar_processor 核心模块
支持不同研究区域（重庆、土耳其等）
"""
import sys
import os
import shutil
from multiprocessing import freeze_support
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from insar_downloader import DownloadConfig, InSARDownloader
from insar_processor import InSARConfig, InSARProcessor


@dataclass
class StudyArea:
    """研究区域配置"""
    name: str
    name_cn: str
    # 区域范围 [lon_min, lat_min, lon_max, lat_max]
    bbox: Tuple[float, float, float, float]
    # 日期范围 [start_date, end_date]
    date_range: Tuple[str, str]
    # 地震位置列表 [(name, lon, lat), ...]
    earthquakes: List[Tuple[str, float, float]]
    # 处理参数
    resolution: float = 120.0
    multilook_wavelength: float = 200.0
    multilook_coarsen: Tuple[int, int] = field(default_factory=lambda: (2, 8))
    detrend_wavelength: float = 4000.0
    # 可选：指定burst列表（用于精确控制数据下载）
    bursts: Optional[List[str]] = None


# 预定义研究区域
STUDY_AREAS = {
    'chongqing': StudyArea(
        name='chongqing',
        name_cn='重庆渝中',
        bbox=(106.2, 29.2, 107.0, 29.9),
        date_range=('2024-05-01', '2024-07-31'),
        earthquakes=[('Chongqing', 106.55, 29.55)],
        resolution=120,
        multilook_wavelength=200,
        multilook_coarsen=(2, 8),
        detrend_wavelength=4000,
    ),
    
    'turkey': StudyArea(
        name='turkey',
        name_cn='土耳其2023地震',
        bbox=(35.5, 35.5, 39.0, 39.0),
        date_range=('2023-01-29', '2023-02-10'),
        earthquakes=[
            ('Turkey_Mw78', 38.11, 37.24),  # Mw 7.8
            ('Turkey_Mw75', 37.17, 37.08),  # Mw 7.5
        ],
        resolution=180,  # 与notebook一致
        multilook_wavelength=400,  # 与notebook一致
        multilook_coarsen=(12, 48),  # 与notebook一致
        detrend_wavelength=300000,  # 300km与notebook一致
    ),
}


def run_study_area(study_area: StudyArea, data_root: str = '/root/insar', 
                   asf_username: str = 'kanezeng', 
                   asf_password: str = '#@!xiaoBOBO123') -> int:
    """
    运行指定研究区域的InSAR处理
    
    核心流程：
    1. 使用 InSARDownloader 下载数据（复用 insar_downloader.py）
    2. 使用 InSARProcessor 处理数据（复用 insar_processor.py）
    """
    
    # 设置路径
    data_dir = Path(data_root) / f'data_{study_area.name}'
    results_dir = Path(data_root) / 'results' / study_area.name
    
    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 70)
    print(f"{study_area.name_cn} InSAR处理")
    print("=" * 70)
    print(f"数据目录: {data_dir}")
    print(f"结果目录: {results_dir}")
    print(f"区域范围: {study_area.bbox}")
    print(f"日期范围: {study_area.date_range}")
    for eq in study_area.earthquakes:
        print(f"地震位置: {eq[0]} ({eq[2]}°N, {eq[1]}°E)")
    
    # ===== 步骤1: 数据下载（复用 insar_downloader.py） =====
    print("\n" + "-" * 70)
    print("步骤1: 数据下载")
    print("-" * 70)
    
    download_config = DownloadConfig(
        lon_min=study_area.bbox[0],
        lat_min=study_area.bbox[1],
        lon_max=study_area.bbox[2],
        lat_max=study_area.bbox[3],
        start_date=study_area.date_range[0],
        end_date=study_area.date_range[1],
        data_dir=str(data_dir),
        asf_username=asf_username,
        asf_password=asf_password,
    )
    
    downloader = InSARDownloader(download_config)
    
    # 验证数据
    print("\n验证数据完整性...")
    status = downloader.verify_data()
    
    if not status['ready_for_processing']:
        print("\n数据不完整，开始下载...")
        
        # 如果指定了burst列表，使用burst下载
        if study_area.bursts:
            print(f"使用burst列表下载（{len(study_area.bursts)}个burst）...")
            # 这里可以扩展burst下载逻辑
            downloader.download_all()  # 回退到普通下载
        else:
            downloader.download_all()
        
        downloader.download_orbits()
    else:
        print("✓ 数据已就绪")
    
    # 再次验证
    status = downloader.verify_data()
    if not status['ready_for_processing']:
        print("✗ 数据准备失败，请检查下载")
        return 1
    
    # ===== 步骤2: InSAR处理（复用 insar_processor.py） =====
    print("\n" + "-" * 70)
    print("步骤2: InSAR处理")
    print("-" * 70)
    
    processor_config = InSARConfig(
        data_dir=str(data_dir),
        work_dir=str(data_dir / 'work'),
        results_dir=str(results_dir),
        resolution=study_area.resolution,
        multilook_wavelength=study_area.multilook_wavelength,
        multilook_coarsen=study_area.multilook_coarsen,
        detrend_wavelength=study_area.detrend_wavelength,
        earthquake_locations=study_area.earthquakes,
    )
    
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
        win_dir = Path(f'/mnt/d/kimi-insar/results/{study_area.name}')
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


def main():
    """主函数 - 可配置运行不同研究区域"""
    
    # 默认运行土耳其2023地震
    # 可以通过命令行参数指定其他区域
    study_area_name = sys.argv[1] if len(sys.argv) > 1 else 'turkey'
    
    if study_area_name not in STUDY_AREAS:
        print(f"错误: 未知研究区域 '{study_area_name}'")
        print(f"可用区域: {', '.join(STUDY_AREAS.keys())}")
        return 1
    
    study_area = STUDY_AREAS[study_area_name]
    
    # 运行处理
    return run_study_area(study_area)


if __name__ == '__main__':
    freeze_support()
    sys.exit(main())
