# 最早加载 .env，供 llm、llm_config 等读取
from config.env_loader import load_env
load_env()
