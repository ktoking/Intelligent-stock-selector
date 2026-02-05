"""
综合分析与评分微调的结构化输出模型，供 LangChain with_structured_output 使用。
减少按行/正则解析的脆弱性。
"""
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class FullAnalysisOutput(BaseModel):
    """full_analysis 单次 LLM 输出的 10 项结构化结果。"""

    core_conclusion: str = Field(description="一句话总结该标的当前是否值得关注及主要理由")
    trend_structure: str = Field(description="一句话描述均线排列与趋势（日K 或 分K）")
    macd_status: str = Field(description="一句话描述 MACD 位置与金叉死叉")
    kdj_status: str = Field(description="一句话描述 KDJ 超买超卖与钝化")
    analysis_reason: str = Field(description="2-4 句综合结论，可结合 PE、期权、均线")
    score: int = Field(ge=1, le=10, description="10=最强，1=最弱")
    score_reason: str = Field(description="一句话说明为何给该评分")
    action: str = Field(description="仅其一：买入 / 观察 / 离场")
    add_price: str = Field(description="加仓参考价，数字如 185.50；无参考时填 —")
    reduce_price: str = Field(description="减仓参考价；无参考时填 —")


class ScoreAdjustment(BaseModel):
    """深度分析后对评分的微调：要么给最终分，要么给 ±1 调整量。"""

    final_score: Optional[int] = Field(default=None, ge=1, le=10, description="最终评分 1-10")
    adjustment: Optional[int] = Field(default=None, ge=-1, le=1, description="相对原分的调整：+1 / 0 / -1")

    @model_validator(mode="after")
    def at_least_one(self):
        if self.final_score is None and self.adjustment is None:
            raise ValueError("final_score 与 adjustment 至少填一个")
        return self
