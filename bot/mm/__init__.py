"""盘口做市的可行性研究：录制公开盘口（recorder）和离线分析（feasibility）。

这一阶段只读公开行情，不下单。recorder 只依赖标准库和 aiohttp，方便单独拷到服务器上跑。
"""
