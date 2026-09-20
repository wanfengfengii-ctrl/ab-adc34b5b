# 粉末衍射晶胞指数化服务（纯后端）

面向同步辐射材料实验室复核粉末衍射数据的**纯后端**立方晶胞指数化服务。
工程师向版本化 JSON 接口提交 4–32 个**严格递增的正有理数**峰位、一个正的
绝对容差和至多 2 个杂质峰额度；服务在精确有理数算术下，把保留峰按序映射到
`N = h² + k² + l²`（`0 ≤ h ≤ k ≤ l ≤ 12`）的**不同可表示值**，并求出未知
正比例因子 λ，使 `|Pᵢ − λ·Nᵢ| ≤ 容差`。

- Python 3.13 + FastAPI，**不提供任何前端/HTML 界面**（Swagger、Redoc 已禁用）。
- 全程 `fractions.Fraction` 精确运算，**没有任何浮点比较**，也**不做贪心配峰**；
  使用带区间传播与单调界剪枝的完整深度优先穷举。
- 结果带 HMAC-SHA256 防篡改证书；复核接口会**重新调用真实求解器**逐项比对，
  而不是只校验签名。

## 运行

```bash
# 构建并启动单个 API（容器内监听 8080）
docker compose up --build

# 自定义宿主机端口
HOST_PORT=9000 docker compose up --build

# 生产环境务必覆盖证书密钥
APP_CERTIFICATE_SECRET="$(openssl rand -hex 32)" docker compose up --build
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok","service":"cell-indexer","version":"1.0.0"}
```

镜像内置 `HEALTHCHECK`，Compose 也声明了等价健康检查。

## 一次性验收入口

验收脚本调用**真实的求解与证书复核实现**（与线上同一份代码），覆盖唯一解、
真实歧义（两组见证四项全平）、证书复核、篡改检测、无解、越界与浮点 token
拒绝等场景。

```bash
# 方式 A：容器外、对正在运行的 API 走真实 HTTP
docker compose up --build -d
python -m app.acceptance --base-url http://localhost:8080

# 方式 B：Compose 一键（自动等待 API 健康后跑验收，退出码即验收结论）
docker compose --profile acceptance up --build --abort-on-container-exit acceptance

# 方式 C：进程内直连真实服务函数（无需启动服务器）
python -m app.acceptance
```

## 接口（版本前缀 `/api/v1`）

### `POST /api/v1/index`

峰位与容差以**十进制字符串**（或分数字符串、整数字面量）给出，以保证数值
精确定义；JSON 浮点 token 一律拒绝（`E1002`）。

```json
{
  "peaks": ["2", "4", "6", "8"],
  "tolerance": "0.01",
  "impurity_quota": 0
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `peaks` | 恰好 4–32 个，严格递增，严格为正的有理数 |
| `tolerance` | 严格为正的有理数（绝对容差，含等号边界） |
| `impurity_quota` | 整数 0–2，默认 0 |

唯一解响应（节选）：

```json
{
  "api_version": "v1",
  "status": "unique",
  "index_limit": 12,
  "request": { "...": "回显的精确输入" },
  "request_fingerprint": "…",
  "witness_count": 1,
  "witnesses": [{
    "scale_factor": "2",
    "impurity_count": 0,
    "impurities": [],
    "omitted_representable_count": 0,
    "max_abs_residual": "0",
    "sum_abs_residual": "0",
    "mappings": [
      {"peak_index": 0, "n": 1, "hkl": [0, 0, 1], "residual": "0"}
    ]
  }],
  "certificate": "…"
}
```

若存在**多组不同映射**在以下四项上**完全同优**，`status` 为 `"ambiguous"`，
`witness_count` 为 2，并返回两组规范见证（按“逐峰 N 序列”字典序最小的两组，
每个 `N` 的 `hkl` 也是规范化三元组）。

### 优化目标（字典序，逐级比较）

1. 杂质峰数量最少；
2. 首、末映射之间**遗漏的可表示值**数量最少；
3. 最大绝对残差最小；
4. 残差绝对值之和最小。

对固定映射，使最大残差最小的因子由精确的带状区间切比雪夫中心唯一确定
（成对公式 `r* = max (aᵢ−aⱼ)/(wᵢ+wⱼ)`，其中 `aᵢ=Pᵢ/Nᵢ`、`wᵢ=1/Nᵢ`），
搜索过程中对该量做增量维护。

### `POST /api/v1/index` 无解

当容差与额度内不存在任何合法正因子映射时，返回 `422` 与稳定机器码，
**响应体不含任何半成品/部分解**：

```json
{"error": {"code": "E2001_NO_SOLUTION", "message": "…"}}
```

### `POST /api/v1/verify`

提交 `{"request": <原始请求>, "result": <index 完整响应>}`。服务依次：

1. 用 HMAC 校验结果文档逐字节未被改动（改动 → `E3002_CERTIFICATE_TAMPERED`）；
2. 校验请求指纹一致（张冠李戴 → `E3003_CERTIFICATE_REQUEST_MISMATCH`）；
3. **重新运行真实求解器**，与文档声称的最优解逐项精确比对。

通过返回 `{"valid": true, ...}`。

## 错误码（稳定机器码，不泄漏内部细节）

| 代码 | 含义 |
| --- | --- |
| `E1001_INVALID_REQUEST` | 请求体非 JSON 对象 / 字段非法 |
| `E1002_INVALID_RATIONAL` | 不是精确定义的有理数（含浮点 token） |
| `E1003_PEAK_COUNT_OUT_OF_RANGE` | 峰数不在 4–32 |
| `E1004_PEAKS_NOT_STRICTLY_INCREASING` | 峰位未严格递增 |
| `E1005_PEAK_POSITION_NOT_POSITIVE` | 峰位非正 |
| `E1006_TOLERANCE_NOT_POSITIVE` | 容差非正 |
| `E1007_IMPURITY_QUOTA_OUT_OF_RANGE` | 杂质额度不在 0–2 |
| `E2001_NO_SOLUTION` | 容差/额度内无解 |
| `E2002_COMPUTATION_LIMIT_EXCEEDED` | 穷举超出计算节点上限（请收窄容差/减少峰数），不返回半成品 |
| `E3001_CERTIFICATE_MALFORMED` | 复核请求结构非法 |
| `E3002_CERTIFICATE_TAMPERED` | 证书签名与文档不符（被改动） |
| `E3003_CERTIFICATE_REQUEST_MISMATCH` | 指纹不符或与重算结果不一致 |
| `E4004_NOT_FOUND` | 未知端点 / 方法不允许 |
| `E5000_INTERNAL_ERROR` | 内部错误（不含敏感信息） |

## 目录

```
app/
  rational.py     精确有理数解析/规范化渲染/确定性 JSON
  solver.py       穷举指数化求解器（Fraction，无浮点、非贪心）
  certificate.py  请求指纹与结果 HMAC 证书
  service.py      输入校验、结果序列化、真实复核
  main.py         FastAPI 路由与统一错误处理
  acceptance.py   一次性验收入口（进程内或 HTTP）
tests/            pytest 单元/端到端测试
Dockerfile        python:3.13-slim，非 root，内置 HEALTHCHECK
docker-compose.yml 单 API + 可配置宿主端口 + acceptance 一次性服务
```

## 本地开发（不使用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --port 8080
```
