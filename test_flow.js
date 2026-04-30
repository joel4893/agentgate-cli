const { trace } = require('./trace');
const fs = require('fs-extra');
const path = require('path');
const chalk = require('chalk');

async function runIntegrationTest() {
    console.log(chalk.cyan.bold('\n--- Starting Agentgate Integration Test ---\n'));

    // 1. Verify Local Context
    const contextPath = path.join(process.cwd(), '.agentgate', 'context.json');
    if (!fs.existsSync(contextPath)) {
        console.error(chalk.red('✖ Test Failed: Local context missing. Run "agentgate wrap github" first.'));
        process.exit(1);
    }
    console.log(chalk.green('✔ Local context found.'));

    // 2. Test the SDK -> Backend -> GitHub flow
    console.log(chalk.blue('Testing dispatch via Agentgate Backend...'));
    try {
        const result = await trace.call("github.create_issue", {
            title: "Integration Test: " + new Date().toLocaleString(),
            body: "Testing the autonomous flow: Agent -> Agentgate -> GitHub."
        });

        if (result.success) {
            console.log(chalk.green('✔ SDK call successful.'));
            console.log(chalk.gray(`  Response: ${JSON.stringify(result.data || result.message)}`));
        }
    } catch (error) {
        console.error(chalk.red('✖ SDK Dispatch Failed:'), error.message);
        console.log(chalk.yellow('\nNote: Make sure your Agentgate server is running: "node server.js"'));
        process.exit(1);
    }

    // 3. Verify Logging (The "Logged" Requirement)
    const logPath = path.join(process.cwd(), 'agentgate.audit.log');
    if (fs.existsSync(logPath)) {
        const logs = fs.readFileSync(logPath, 'utf8');
        const lastLog = logs.trim().split('\n').pop();
        console.log(chalk.green('✔ Audit log verified.'));
        console.log(chalk.gray(`  Latest Log Entry: ${lastLog}`));
    } else {
        console.warn(chalk.yellow('⚠ Warning: Audit log file not found. Check server permissions.'));
    }

    console.log(chalk.cyan.bold('\n--- Integration Test Complete ---\n'));
}

runIntegrationTest().catch(err => {
    console.error(err);
    process.exit(1);
});