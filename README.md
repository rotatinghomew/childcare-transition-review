# 托育适应期的过渡结果与继续预约

这里保存托育适应期的过渡结果与继续预约工作所用的匿名合成记录。文件不包含分析结果或居民个人资料。

## 资料

`samples/` 中每个文件都是独立的 UTF-8 JSON 文档：`children`、`timeline`、`calendar`、`capacity`、`staff`、`review`。日期是当地日历日期。空值表示没有对应观测或状态资料，不是数值零。

cohort 是首次接受日期；reported 为园方登记日。周几时段为园方营业标签，以 calendar 为准；capacity 列的是房间和教师同时满足时的每场容量。连续承接按四周核对，reserved 中实际确认按登记日起生效。

## 运行

使用 Python 3.11，无需第三方依赖或服务。`python3 inspect_samples.py` 逐个读取资料并列出记录数；`python3 -m unittest -v` 检查关键原始记录间的关系。输出只描述原始资料，不是业务比较结果。
