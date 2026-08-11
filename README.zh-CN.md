# Agent Skills

[![Test](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml/badge.svg)](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一组可复用的 Agent Skills，用于审查、合并和发布托管在 Gitea 上的仓库。

[English](README.md)

## 可用技能

| Skill | 用途 |
| --- | --- |
| `gitea-review` | 审查或复审 Gitea Pull Request，并提交正式 Review。 |
| `gitea-merge` | 校验普通 Pull Request，并使用 rebase、squash 或 merge commit 合并。 |
| `gitea-release` | 合并 Release Please Pull Request，并验证 Workflow、Tag 和 Gitea Release。 |

三个 Skill 均可独立安装和使用。`gitea-release` 不依赖 `gitea-merge`，并且只处理已经
存在的 Release Please Pull Request。

`gitea-review` 会先根据元数据判断 PR 规模，再交给一个不继承当前对话且可复用的 worker。
快速和聚焦复审使用 medium reasoning，中型 PR 使用 high，大型 PR 使用 xhigh；父 Agent
不会读取 patch。

## 性能评测

2026-08-12 使用隔离 Docker 基准环境，对每个工作流分别执行了 3 组 Skill/baseline
配对评测；模型为 `gpt-5.6-sol`、medium reasoning，镜像为
`agent-skills-codex-benchmark:0.147.0`。每条 lane 都使用全新的 Codex tmpfs、Gitea
实例和仓库，并接收完全相同的 PR 元数据与 commit SHA。18 次运行及服务端状态校验全部
通过。

| Skill | 有效配对 | 平均耗时，Skill / baseline | 耗时 | 输入 Token | 输出 Token | 顶层调用中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gitea-merge` | 3/3 | 38.5s / 51.2s | -24.8% | -43.6% | -26.4% | 2 / 4 |
| `gitea-review` | 3/3 | 60.4s / 72.4s | -16.6% | -48.1% | -66.5% | 3* / 9 |
| `gitea-release` | 3/3 | 44.7s / 76.9s | -41.8% | -60.7% | -51.1% | 3 / 7 |

每轮耗时按 Skill / baseline 展示；负数表示 Skill 更快：

| Skill | 第 1 轮 | 第 2 轮 | 第 3 轮 |
| --- | ---: | ---: | ---: |
| `gitea-merge` | 44.7s / 61.8s (-27.7%) | 43.7s / 44.3s (-1.4%) | 27.2s / 47.6s (-42.8%) |
| `gitea-review` | 55.1s / 104.4s (-47.2%) | 54.7s / 63.0s (-13.0%) | 71.3s / 49.8s (+43.1%) |
| `gitea-release` | 40.3s / 79.1s (-49.1%) | 47.7s / 72.7s (-34.4%) | 46.2s / 78.8s (-41.3%) |

宿主机只挂载 `auth.json`；Skill lane 额外以只读方式挂载目标 Skill，因此本地记忆与缓存
已隔离，但服务端 prompt cache 无法禁用：返回的 cached input token 不为零，且已包含在
上表的 Token 对比中。runner 会随机调整 lane 顺序。`gitea-review` 子 Agent 的内部事件
不会出现在父 Agent JSONL 中，因此带星号的调用次数只统计顶层事件。复现方法见
[性能评测说明](tests/benchmark/README.md)。

## 环境要求

- 支持 [Agent Skills](https://agentskills.io/) 格式的 Agent 运行环境
- Git 和 Python 3.11 或更高版本
- 已为目标 Gitea 实例配置的 [Tea](https://gitea.com/gitea/tea)
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

三个 Skill 都需要通过网络访问 Gitea。在受管理的沙盒中，Agent 应在第一次执行脚本时
直接申请网络提权，不应先在受限沙盒中运行 Tea。脚本可以直接执行，因此长期授权可以
限制到具体脚本，而不是放开通用 Python 解释器。

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
│   └── gitea-review/
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
