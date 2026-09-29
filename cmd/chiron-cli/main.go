package main

import (
	"errors"
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "chiron",
	Short: "chiron CLI - AI Agent Platform Management Tool",
	Long:  `Chiron CLI provides commands to manage Chiron services, instances, configuration, and health checks.`,
}

func init() {
	// Add subcommands
	rootCmd.AddCommand(startCmd)
	rootCmd.AddCommand(stopCmd)
	rootCmd.AddCommand(statusCmd)
	rootCmd.AddCommand(healthCmd)
	rootCmd.AddCommand(configCmd)
	rootCmd.AddCommand(instanceCmd)
	rootCmd.AddCommand(dbCmd)
	rootCmd.AddCommand(logsCmd)
	rootCmd.AddCommand(runCmd)
}

func main() {
	if err := rootCmd.Execute(); err != nil {
		// run 子命令用具体退出码表达"为何结束"（超时/被拦下/预算越界），
		// 其它命令的错误按惯例退出 1。
		var ec *exitCodeError
		if errors.As(err, &ec) {
			os.Exit(ec.code)
		}
		fmt.Println(err)
		os.Exit(1)
	}
}
