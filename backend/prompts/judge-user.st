# 本次生成目标与约束（JSON，独立于待评审内容）
{{generation_constraints}}

# 评分标准（rubric）
{{rubric}}

# 待质检内容（JSON）
{{payload}}

# 任务
请依据上方评分标准，对内容逐维度打分（0-100），输出格式：
{"dimension_scores": {"<维度id>": <0-100>, ...}}
