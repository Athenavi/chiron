package main

import (
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/athenavi/chiron/internal/cli"
	"github.com/spf13/cobra"
)

// runCmd 是 headless 非交互入口（方案 03 §2「批 F」）：
// 复用网关既有的 /submit + /events(SSE)，把一次 agent 回合跑完并把结果交给 CI/脚本。
var runCmd = &cobra.Command{
	Use:   "run",
	Short: "Run a headless (non-interactive) agent turn",
	Long: `Run a single agent turn against the Chiron gateway without a UI.

It subscribes to the gateway SSE stream (GET /events), submits the message
(POST /submit), and streams events until the turn finishes. No new server
endpoints are introduced — this is a thin client over the existing API.

Exit codes:
  0  completed
  1  other failure (engine error, network/protocol error)
  2  blocked by a guardrail, or requires approval/answer that the policy refused
  3  timed out (--timeout)
  4  budget exceeded (budget_exceeded:<axis>)

By default the turn *fails* (exit 2) when the agent needs approval or asks a
question: silently auto-approving in CI is a dangerous default.`,
	Example: `  chiron-cli run --session demo --message "hello"
  chiron-cli run --session demo --message "deploy" --on-approval approve
  chiron-cli run --session demo --message "hello" --json | jq -c .type`,
	Args:          cobra.NoArgs,
	SilenceUsage:  true,
	SilenceErrors: true,
	RunE:          runRun,
}

var (
	runAddr       string
	runAPIKey     string
	runSession    string
	runMessage    string
	runJSON       bool
	runTimeoutSec int
	runOnApproval string
	runOnAsk      string
)

func init() {
	runCmd.Flags().StringVar(&runAddr, "addr", "http://localhost:8080", "Gateway base URL")
	runCmd.Flags().StringVar(&runAPIKey, "api-key", "",
		"Gateway API Key sent as X-API-Key (falls back to $CHIRON_API_KEY)")
	runCmd.Flags().StringVar(&runSession, "session", "", "Session ID to run the turn in (required)")
	runCmd.Flags().StringVar(&runMessage, "message", "", "User message to submit (required)")
	runCmd.Flags().BoolVar(&runJSON, "json", false,
		"Emit NDJSON (one line per SSE event) instead of the final text")
	runCmd.Flags().IntVar(&runTimeoutSec, "timeout", 600, "Overall timeout in seconds")
	runCmd.Flags().StringVar(&runOnApproval, "on-approval", string(cli.OnApprovalFail),
		"What to do when a tool needs approval: fail|approve|deny")
	runCmd.Flags().StringVar(&runOnAsk, "on-ask", string(cli.OnAskFail),
		"What to do when the agent asks a question: fail|default")
}

// exitCodeError 让 run 命令把 cli 的退出码带出 RunE，
// 由 main 统一 os.Exit(code) —— 而不是让 cobra 一律退出 1。
type exitCodeError struct{ code int }

func (e *exitCodeError) Error() string { return fmt.Sprintf("exit status %d", e.code) }

func runRun(cmd *cobra.Command, args []string) error {
	if runSession == "" {
		return errors.New("--session is required")
	}
	if runMessage == "" {
		return errors.New("--message is required")
	}

	onApproval := cli.OnApproval(runOnApproval)
	onAsk := cli.OnAsk(runOnAsk)
	if err := cli.ValidateStrategies(onApproval, onAsk); err != nil {
		return err
	}

	apiKey := runAPIKey
	if apiKey == "" {
		apiKey = os.Getenv("CHIRON_API_KEY")
	}

	code := cli.NewRunner().Run(cmd.Context(), cli.RunOptions{
		Addr:       runAddr,
		APIKey:     apiKey,
		SessionID:  runSession,
		Message:    runMessage,
		JSON:       runJSON,
		Timeout:    time.Duration(runTimeoutSec) * time.Second,
		OnApproval: onApproval,
		OnAsk:      onAsk,
	})
	if code == cli.ExitOK {
		return nil
	}
	return &exitCodeError{code: code}
}
