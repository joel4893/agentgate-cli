const express = require('express');
const RuleEngine = require('./engine');
const chalk = require('chalk');
const app = express();
app.use(express.json());

app.post('/v1/dispatch', async (req, res) => {
    const { action, repository, parameters } = req.body;

    // 1. Deterministic Validation
    const check = RuleEngine.validate(action, repository, parameters);

    if (!check.valid) {
        console.error(chalk.red(`[Policy Violation] ${check.reason}`));
        return res.status(403).json({
            success: false,
            error: "Policy Violation",
            details: check.reason
        });
    }

    // 2. If valid, proceed to LLM reasoning or direct execution
    console.log(chalk.green(`[Agentgate] Action approved by engine. Dispatching...`));
    
    // ... existing GitHub API logic ...
    res.json({ success: true, message: "Action executed" });
});

app.listen(3000, () => console.log('Server running on port 3000'));