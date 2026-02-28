#!/usr/bin/env python3
"""
InSAR Data Downloader - InSAR数据下载模块
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime
import logging
import warnings

warnings.filterwarnings('ignore')

# 导入pygmtsar
try:
    from pygmtsar import S1, Stack
except ImportError:
    S1 = None
    Stack = None

from insar_processor import InSARConfig

# 导入tqdm和asf_search
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


@dataclass
class DownloadConfig:
    """下载配置类 - 默认重庆区域(大范围避免chunk错误)"""
    lon_min: float = 106.2  # 扩大范围
    lon_max: float = 107.0  # 扩大范围
    lat_min: float = 29.2   # 扩大范围
    lat_max: float = 29.9   # 扩大范围
    start_date: str = '2024-05-01'
    end_date: str = '2024-07-31'
    satellite: str = 'S1A'
    polarization: str = 'VV'
    download_poeorb: bool = True
    data_dir: str = 'data_proc'
    asf_username: str = ''
    asf_password: str = ''
    
    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)
    
    @property
    def date_range(self) -> Tuple[datetime, datetime]:
        start = datetime.strptime(self.start_date, '%Y-%m-%d')
        end = datetime.strptime(self.end_date, '%Y-%m-%d')
        return (start, end)


class InSARDownloader:
    """InSAR数据下载器类"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self._setup_logging()
        self.data_path = Path(config.data_dir)
        self.data_path.mkdir(exist_ok=True)
        
        self.logger.info(f"数据下载器初始化完成")
        self.logger.info(f"数据目录: {self.data_path.absolute()}")
    
    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def download_slc_data(self) -> List[str]:
        """下载Sentinel-1 SLC数据（使用asf_search）"""
        self.logger.info("="*70)
        self.logger.info("开始下载SLC数据...")
        self.logger.info("="*70)
        
        downloaded_files = []
        
        try:
            import asf_search
            
            start_dt, end_dt = self.config.date_range
            bbox = self.config.bbox
            wkt = f'POLYGON(({bbox[0]} {bbox[1]}, {bbox[2]} {bbox[1]}, {bbox[2]} {bbox[3]}, {bbox[0]} {bbox[3]}, {bbox[0]} {bbox[1]}))'
            
            # 搜索
            self.logger.info("正在搜索ASF数据库...")
            results = asf_search.search(
                platform=asf_search.PLATFORM.SENTINEL1,
                processingLevel=asf_search.PRODUCT_TYPE.SLC,
                start=start_dt,
                end=end_dt,
                intersectsWith=wkt
            )
            
            if len(results) == 0:
                self.logger.warning("未找到SLC数据")
                return []
            
            self.logger.info(f"找到 {len(results)} 个场景")
            
            # 严格限制：只选择2个场景（最早和最晚日期）
            # 按日期排序
            sorted_results = sorted(results, key=lambda r: r.properties.get('startTime', ''))
            
            if len(sorted_results) >= 2:
                # 只选择第一个（最早）和最后一个（最晚）
                results = [sorted_results[0], sorted_results[-1]]
                self.logger.info(f"限制下载: 只选择2个场景（{sorted_results[0].properties.get('startTime', '')[:10]} 和 {sorted_results[-1].properties.get('startTime', '')[:10]}）")
            else:
                self.logger.info(f"场景数量不足，使用所有 {len(sorted_results)} 个场景")
            
            # 配置认证
            session = asf_search.ASFSession()
            if self.config.asf_username and self.config.asf_password:
                self.logger.info(f"认证用户: {self.config.asf_username}")
                session.auth_with_creds(
                    self.config.asf_username,
                    self.config.asf_password
                )
            
            # 下载所有场景（带进度条）
            iterator = tqdm(results, desc="下载SLC") if tqdm else results
            for product in iterator:
                scene_name = product.properties['sceneName']
                try:
                    product.download(path=self.config.data_dir, session=session)
                    downloaded_files.append(scene_name)
                    self.logger.info(f"✓ {scene_name}")
                except Exception as e:
                    self.logger.error(f"✗ {scene_name}: {e}")
            
            self.logger.info(f"SLC下载完成: {len(downloaded_files)}/{len(results)}")
            
        except ImportError:
            self.logger.error("asf_search库未安装")
        except Exception as e:
            self.logger.error(f"SLC下载出错: {e}")
        
        return downloaded_files
    
    def download_dem(self) -> Optional[str]:
        """下载DEM数据 - 需要先扫描SLC数据"""
        self.logger.info("="*70)
        self.logger.info("开始下载DEM...")
        self.logger.info("="*70)
        
        dem_path = self.data_path / 'dem.nc'
        if dem_path.exists():
            self.logger.info(f"DEM已存在: {dem_path}")
            return str(dem_path)
        
        try:
            # 检查是否有SLC数据
            scenes = S1.scan_slc(self.config.data_dir, polarization=self.config.polarization)
            if len(scenes) == 0:
                self.logger.warning("未找到SLC数据，无法下载DEM")
                return None
            
            self.logger.info(f"发现 {len(scenes)} 个SLC场景，使用Stack下载DEM...")
            
            # 创建Stack并设置场景
            temp_dir = self.data_path / 'temp_dem_dl'
            temp_dir.mkdir(exist_ok=True)
            
            sbas = Stack(str(temp_dir), drop_if_exists=True).set_scenes(scenes)
            
            # 现在可以获取DEM
            self.logger.info("正在从SRTM3下载DEM...")
            dem = sbas.get_dem()
            
            # 保存
            dem.to_netcdf(dem_path)
            self.logger.info(f"✓ DEM下载完成: {dem_path}")
            
            # 清理临时目录
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            return str(dem_path)
            
        except Exception as e:
            self.logger.warning(f"DEM下载失败: {e}")
            return None
    
    def download_landmask(self) -> Optional[str]:
        """下载陆地掩膜 - 需要先扫描SLC数据"""
        self.logger.info("="*70)
        self.logger.info("开始生成Landmask...")
        self.logger.info("="*70)
        
        landmask_path = self.data_path / 'landmask.nc'
        if landmask_path.exists():
            self.logger.info(f"Landmask已存在: {landmask_path}")
            return str(landmask_path)
        
        try:
            # 检查是否有SLC数据
            scenes = S1.scan_slc(self.config.data_dir, polarization=self.config.polarization)
            if len(scenes) == 0:
                self.logger.warning("未找到SLC数据，无法生成Landmask")
                return None
            
            self.logger.info(f"发现 {len(scenes)} 个SLC场景，使用Stack生成Landmask...")
            
            # 创建Stack并设置场景
            temp_dir = self.data_path / 'temp_landmask_dl'
            temp_dir.mkdir(exist_ok=True)
            
            sbas = Stack(str(temp_dir), drop_if_exists=True).set_scenes(scenes)
            
            # 现在可以获取Landmask
            self.logger.info("正在生成Landmask...")
            landmask = sbas.get_landmask()
            
            # 保存
            landmask.to_netcdf(landmask_path)
            self.logger.info(f"✓ Landmask完成: {landmask_path}")
            
            # 清理临时目录
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            return str(landmask_path)
            
        except Exception as e:
            self.logger.warning(f"Landmask失败: {e}")
            return None
    
    def download_orbits(self) -> List[str]:
        """下载精密轨道文件"""
        self.logger.info("="*70)
        self.logger.info("开始下载轨道文件...")
        self.logger.info("="*70)
        
        try:
            scenes = S1.scan_slc(self.config.data_dir, polarization=self.config.polarization)
            if len(scenes) == 0:
                self.logger.warning("未找到SLC文件")
                return []
            
            self.logger.info(f"找到 {len(scenes)} 个场景")
            
            # 检查哪些场景缺少轨道文件
            missing_orbits = scenes[scenes.orbitpath.isna()]
            if len(missing_orbits) > 0:
                self.logger.info(f"{len(missing_orbits)} 个场景需要下载轨道文件")
                self.logger.info("正在下载精密轨道文件...")
                
                # 使用S1.download_orbits下载
                S1.download_orbits(str(self.data_path), scenes, n_jobs=4)
                self.logger.info("✓ 轨道文件下载完成")
            else:
                self.logger.info("✓ 所有场景已有轨道文件")
            
            # 返回下载的轨道文件列表
            orbit_files = list(self.data_path.glob('*.EOF'))
            return [str(f) for f in orbit_files]
            
        except Exception as e:
            self.logger.error(f"轨道文件下载出错: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return []
    
    def verify_data(self) -> Dict[str, Any]:
        """验证数据完整性 - 根据日期+地区判断数据是否匹配"""
        self.logger.info("="*70)
        self.logger.info("验证数据完整性 (日期+地区匹配检查)...")
        self.logger.info(f"期望区域: [{self.config.lon_min}, {self.config.lat_min}, {self.config.lon_max}, {self.config.lat_max}]")
        self.logger.info(f"期望日期: {self.config.start_date} ~ {self.config.end_date}")
        self.logger.info("="*70)
        
        import pandas as pd
        
        status = {
            'slc_count': 0,
            'slc_matching': 0,
            'dem_exists': False,
            'dem_matching': False,
            'landmask_exists': False,
            'landmask_matching': False,
            'region_match': False,
            'date_match': False,
            'ready_for_processing': False,
            'expected': {
                'lon_range': (self.config.lon_min, self.config.lon_max),
                'lat_range': (self.config.lat_min, self.config.lat_max),
                'date_range': (self.config.start_date, self.config.end_date),
            },
            'actual': {}
        }
        
        # 检查SLC - 验证日期和地区匹配
        try:
            scenes = S1.scan_slc(self.config.data_dir, polarization=self.config.polarization)
            status['slc_count'] = len(scenes)
            
            if len(scenes) > 0:
                # 获取场景日期范围
                scene_dates = pd.to_datetime(scenes.index)
                start_date = pd.to_datetime(self.config.start_date)
                end_date = pd.to_datetime(self.config.end_date)
                
                # 检查日期匹配
                matching_by_date = scenes[
                    (scene_dates >= start_date) & 
                    (scene_dates <= end_date)
                ]
                status['slc_matching'] = len(matching_by_date)
                status['date_match'] = len(matching_by_date) >= 2
                
                # 获取实际日期范围
                status['actual']['date_range'] = (
                    str(scene_dates.min().date()),
                    str(scene_dates.max().date())
                )
                
                self.logger.info(f"SLC文件: {len(scenes)} 个")
                self.logger.info(f"  日期匹配: {len(matching_by_date)} 个 ({status['actual']['date_range'][0]} ~ {status['actual']['date_range'][1]})")
            else:
                self.logger.info(f"SLC文件: 0 个")
        except Exception as e:
            self.logger.warning(f"无法扫描SLC: {e}")
        
        # 检查DEM - 验证范围匹配 (支持大小写不敏感)
        dem_path = self.data_path / 'dem.nc'
        if not dem_path.exists():
            dem_path = self.data_path / 'DEM.nc'  # 尝试大写
        if dem_path.exists():
            status['dem_exists'] = True
            try:
                import xarray as xr
                dem_ds = xr.open_dataset(str(dem_path))
                # 变量名可能是'z'或'DEM'
                dem_var = dem_ds.z if 'z' in dem_ds.data_vars else dem_ds.DEM
                dem_lon_min, dem_lon_max = float(dem_ds.lon.min()), float(dem_ds.lon.max())
                dem_lat_min, dem_lat_max = float(dem_ds.lat.min()), float(dem_ds.lat.max())
                
                # 检查DEM范围是否匹配期望区域 (容差0.5度)
                lon_match = (abs(dem_lon_min - self.config.lon_min) < 0.5 and 
                           abs(dem_lon_max - self.config.lon_max) < 0.5)
                lat_match = (abs(dem_lat_min - self.config.lat_min) < 0.5 and 
                           abs(dem_lat_max - self.config.lat_max) < 0.5)
                
                status['dem_matching'] = lon_match and lat_match
                status['actual']['dem_bbox'] = (dem_lon_min, dem_lat_min, dem_lon_max, dem_lat_max)
                
                match_str = '✓' if status['dem_matching'] else '✗ (范围不匹配)'
                self.logger.info(f"DEM文件: ✓ {match_str}")
                self.logger.info(f"  范围: [{dem_lon_min:.2f}, {dem_lat_min:.2f}, {dem_lon_max:.2f}, {dem_lat_max:.2f}]")
                dem_ds.close()
            except Exception as e:
                self.logger.info(f"DEM文件: ✓ (无法验证范围: {e})")
        else:
            self.logger.info(f"DEM文件: ✗")
        
        # 检查Landmask (支持大小写不敏感)
        landmask_path = self.data_path / 'landmask.nc'
        if not landmask_path.exists():
            landmask_path = self.data_path / 'LANDMASK.nc'
        if landmask_path.exists():
            status['landmask_exists'] = True
            status['landmask_matching'] = status['dem_matching']  # 假设同时生成
            match_str = '✓' if status['landmask_matching'] else '✗ (范围不匹配)'
            self.logger.info(f"Landmask: ✓ {match_str}")
        else:
            self.logger.info(f"Landmask: ✗")
        
        # 综合判断 - 需要日期和地区都匹配
        status['region_match'] = status['dem_matching'] and status['landmask_matching']
        status['ready_for_processing'] = (
            status['slc_matching'] >= 2 and
            status['dem_matching'] and
            status['landmask_matching']
        )
        
        self.logger.info("-"*70)
        if status['ready_for_processing']:
            self.logger.info("✓ 数据完整且匹配")
        elif status['slc_count'] >= 2 and status['dem_exists'] and status['landmask_exists']:
            self.logger.warning("⚠ 数据存在但不匹配指定区域/日期")
            self.logger.warning(f"  SLC日期: 期望 {self.config.start_date}~{self.config.end_date}, 实际 {status['actual'].get('date_range', '未知')}")
            self.logger.warning(f"  DEM范围: 期望 [{self.config.lon_min}, {self.config.lat_min}, {self.config.lon_max}, {self.config.lat_max}]")
            if 'dem_bbox' in status['actual']:
                self.logger.warning(f"           实际 [{status['actual']['dem_bbox'][0]:.2f}, {status['actual']['dem_bbox'][1]:.2f}, {status['actual']['dem_bbox'][2]:.2f}, {status['actual']['dem_bbox'][3]:.2f}]")
        else:
            self.logger.warning("✗ 数据不完整")
        
        return status
    
    def download_all(self) -> Dict[str, Any]:
        """下载所有数据 - 注意顺序：先SLC，后DEM/Landmask"""
        self.logger.info("="*70)
        self.logger.info("开始完整数据下载")
        self.logger.info("="*70)
        
        results = {
            'slc_files': [],
            'dem_file': None,
            'landmask_file': None,
            'orbit_files': [],
            'success': False
        }
        
        # 第1步：下载SLC数据（必须先有SLC才能获取DEM和Landmask）
        results['slc_files'] = self.download_slc_data()
        
        # 第2步：下载DEM（需要SLC数据）
        results['dem_file'] = self.download_dem()
        
        # 第3步：下载Landmask（需要SLC数据）
        results['landmask_file'] = self.download_landmask()
        
        # 第4步：下载轨道文件
        if self.config.download_poeorb:
            results['orbit_files'] = self.download_orbits()
        
        status = self.verify_data()
        results['success'] = status['ready_for_processing']
        
        if results['success']:
            self.logger.info("="*70)
            self.logger.info("✓ 所有数据下载完成！")
            self.logger.info("="*70)
        else:
            self.logger.error("="*70)
            self.logger.error("✗ 数据下载未完成")
            self.logger.error("="*70)
        
        return results
    
    def create_processor_config(self) -> InSARConfig:
        """创建处理器配置"""
        center_lon = (self.config.lon_min + self.config.lon_max) / 2
        center_lat = (self.config.lat_min + self.config.lat_max) / 2
        
        return InSARConfig(
            work_dir='raw_kahr',
            data_dir=self.config.data_dir,
            results_dir='results',
            dem_file='dem.nc',
            landmask_file='landmask.nc',
            polarization=self.config.polarization,
            resolution=180.0,
            earthquake_locations=[('Target', center_lon, center_lat)]
        )


def main():
    """主函数"""
    config = DownloadConfig(
        lon_min=36.5, lon_max=38.5,
        lat_min=36.0, lat_max=38.5,
        start_date='2023-01-01',
        end_date='2023-03-01',
        polarization='VV',
        download_poeorb=True,
        asf_username='kanezeng',
        asf_password='#@!xiaoBOBO123'
    )
    
    downloader = InSARDownloader(config)
    results = downloader.download_all()
    
    if results['success']:
        print("\n数据下载成功！")
    else:
        print("\n数据下载失败")


if __name__ == '__main__':
    main()
