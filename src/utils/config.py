"""
配置工具
"""
import yaml
import os
from typing import Dict, Optional


def _load_dotenv_once():
    """启动时自动加载项目根目录的 .env 文件到环境变量。

    让 API Key 等敏感配置集中放在一个 .env 文件里,无需每次手动 set 环境变量。
    已存在的环境变量优先(不覆盖),未安装 python-dotenv 时静默跳过。
    """
    try:
        from dotenv import load_dotenv, find_dotenv
    except Exception:
        return
    try:
        # 先找当前目录及上层的 .env;找不到再试项目根(config.py 上溯两级)
        path = find_dotenv(usecwd=True)
        if not path:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            candidate = os.path.join(root, '.env')
            path = candidate if os.path.exists(candidate) else ''
        if path:
            load_dotenv(path, override=False)
    except Exception:
        pass


_load_dotenv_once()


class Config:
    """配置类"""

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置

        Args:
            config_path: 配置文件路径
        """
        self.config = {}

        if config_path and os.path.exists(config_path):
            self.load_config(config_path)

    def load_config(self, config_path: str):
        """
        加载配置文件
        
        Args:
            config_path: 配置文件路径
        """
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
    
    def get(self, key: str, default=None):
        """
        获取配置项
        
        Args:
            key: 配置键
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            
            if value is None:
                return default
        
        return value
    
    def set(self, key: str, value):
        """
        设置配置项
        
        Args:
            key: 配置键
            value: 配置值
        """
        keys = key.split('.')
        config = self.config
        
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        config[keys[-1]] = value
    
    def save_config(self, config_path: str):
        """
        保存配置文件
        
        Args:
            config_path: 配置文件路径
        """
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
    
    @staticmethod
    def load_example_config():
        """加载示例配置"""
        return Config('config/config.example.yaml')