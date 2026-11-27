# Agent Skills

[![Test](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml/badge.svg)](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一组可复用的 Agent Skills，用于组织工作区，以及审查、合并和发布托管在 Gitea 上的仓库。

[English](README.md)

## 可用技能

| Skill | 用途 |
| --- | --- |
| `gitea-review` | 审查或复审 Gitea Pull Request，并提交正式 Review。 |
| `gitea-merge` | 通过受门禁保护的 rebase、squash 或 merge commit 合并普通 Pull Request；squash 标题兼容 Release Please，且默认删除已合并的特性分支。 |
| `gitea-release` | 合并 Release Please Pull Request，并验证 Workflow、Tag 和 Gitea Release。 |
| `organize-workspace` | 规划或盘点多项目与运维工作区、判断文件归属，并按授权执行目录整理。 |

各 Skill 均可独立安装和使用。`gitea-release` 不依赖 `gitea-merge`，并且只处理已经
存在的 Release Please Pull Request。

`gitea-review` 会先根据元数据判断 PR 规模，再交给一个不继承当前对话且可复用的 worker。
快速和聚焦复审要求标准档（`standard`），中型 PR 要求进阶档（`advanced`），大型 PR
要求最高能力档（`highest`）。Agent 从当前运行环境的可用模型中自主选择，并尊重用户
指定的模型与约束；技能不固定模型名或推理强度。父 Agent 不会读取 patch。

`organize-workspace` 是纯指导型技能，没有辅助脚本，也不依赖 Gitea。它尊重既有布局，
保留仓库边界与历史证据，并区分归属建议、只读盘点、规划和已授权迁移。
任务导航按规模分层，相关记录按需读取，避免将全部历史清单放入根 README。
调用示例：“使用 $organize-workspace 只读盘点当前工作区并给出布局建议，不移动文件。”

## 性能评测

2026-11-27（基线提交 `5dddb20`）使用隔离 Docker 基准环境，对三个 Gitea 工作流分别
执行了 3 组 Skill/baseline 配对评测；本轮受控对照选用 `gpt-6.2-sol`、medium
reasoning（仅为该轮评测选定，并非技能或脚本默认配置），镜像为
`agent-skills-codex-benchmark:0.172.0`。`organize-workspace` 为纯指导型技能，不在
该自动化 Gitea 评测范围内。每条 lane 都使用全新的 Codex tmpfs、Gitea 实例和仓库，
并接收完全相同的 PR 元数据与 commit SHA。18 次运行及服务端状态校验全部通过。

| Skill | 有效配对 | 平均耗时，Skill / baseline | 耗时 | 输入 Token | 输出 Token | 顶层调用中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gitea-merge` | 3/3 | 18.1s / 32.2s | -43.6% | -45.5% | -66.4% | 2 / 5 |
| `gitea-review` | 3/3 | 40.5s / 45.4s | -10.7% | -35.6% | -60.7% | 3* / 9 |
| `gitea-release` | 3/3 | 20.2s / 43.3s | -53.3% | -36.0% | -76.3% | 3 / 9 |

每轮耗时按 Skill / baseline 展示；负数表示 Skill 更快：

| Skill | 第 1 轮 | 第 2 轮 | 第 3 轮 |
| --- | ---: | ---: | ---: |
| `gitea-merge` | 19.2s / 31.9s (-39.9%) | 17.6s / 33.8s (-48.1%) | 17.7s / 30.8s (-42.7%) |
| `gitea-review` | 41.9s / 41.9s (+0.0%) | 40.4s / 57.1s (-29.2%) | 39.1s / 37.0s (+5.5%) |
| `gitea-release` | 20.3s / 40.9s (-50.4%) | 19.6s / 50.9s (-61.4%) | 20.8s / 38.2s (-45.6%) |

宿主机只挂载 `auth.json`；Skill lane 额外以只读方式挂载目标 Skill，因此本地记忆与缓存
已隔离，但服务端 prompt cache 无法禁用：返回的 cached input token 不为零，且已包含在
上表的 Token 对比中。runner 会随机调整 lane 顺序。`gitea-review` 子 Agent 的内部事件
不会出现在父 Agent JSONL 中，因此带星号的调用次数及 Token 统计只覆盖顶层事件。复现
方法见 [性能评测说明](tests/benchmark/README.md)。

## 环境要求

- 支持 [Agent Skills](https://agentskills.io/) 格式的 Agent 运行环境
- Gitea 技能需要 Git、Python 3.11 或更高版本，以及已为目标实例配置的
  [Tea](https://gitea.com/gitea/tea)
- `organize-workspace` 盘点或迁移时需要文件系统访问，检查仓库时需要 Git；不依赖 Python 或 Tea
- 使用 `skills` CLI 安装时需要 Node.js 和 npm

凭据只保存在 Tea 的本地凭据存储中。Skill 不要求将 Token、密码或私钥提交到本仓库。

## 安装

为 Codex 全局安装全部 Skill：

```bash
npx skills add kuddy-on/agent-skills --skill '*' --global --agent codex --yes
```

安装单个 Skill：

```bash
npx skills add kuddy-on/agent-skills --skill gitea-merge --global --agent codex --yes
```

## 更新

```bash
npx skills update --global --yes
```

更新命令只刷新已经安装的 Skill。仓库新增或重命名 Skill 后，需要重新运行安装命令。

## 沙盒权限

三个 Gitea Skill 需要通过网络访问 Gitea。在受管理的沙盒中，Agent 应在第一次执行脚本时
直接申请网络提权，不应先在受限沙盒中运行 Tea。脚本可以直接执行，因此长期授权可以
限制到具体脚本，而不是放开通用 Python 解释器。

`organize-workspace` 只需取得本次盘点或迁移所需的访问权限。请求布局建议不授权修改
文件系统或操作服务。

## 开发

先安装开发工具（ShellCheck 使用操作系统的软件包管理器安装），再运行格式与静态检查：

```bash
python -m pip install --requirement requirements-dev.txt
go install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1
scripts/check.sh
```

运行单元测试：

```bash
python -m unittest discover -s tests -v
```

运行隔离集成测试：

```bash
tests/integration/run.sh
```

集成测试会创建临时本地 Gitea 容器和仓库，执行一次真实的 Pull Request 合并，并在结束
时销毁全部临时资源。它不会使用已配置的生产 Gitea 账号。

## 仓库结构

```text
agent-skills/
├── scripts/
├── skills/
│   ├── gitea-merge/
│   ├── gitea-release/
│   ├── gitea-review/
│   └── organize-workspace/
├── tests/
├── AGENTS.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── README.zh-CN.md
```

## 贡献与支持

提交 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，使用问题请参考
[SUPPORT.md](SUPPORT.md)。安全漏洞请按照 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

本项目使用 [MIT License](LICENSE)。
