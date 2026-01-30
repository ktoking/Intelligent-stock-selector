import warnings
warnings.filterwarnings("ignore", module="urllib3")

from agents.fundamental import analyze_fundamental

if __name__ == "__main__":
    try:
        result = analyze_fundamental("AAPL")
        print(result)
    except ValueError as e:
        print(f"配置错误: {e}")
        exit(1)
    except RuntimeError as e:
        print(f"API 错误: {e}")
        exit(1)
    except Exception as e:
        print(f"运行错误: {e}")
        exit(1)
