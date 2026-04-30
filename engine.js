const chalk = require('chalk');

/**
 * Deterministic Rule Engine for Agentgate
 * This handles strict logic that the LLM is not allowed to override.
 */
const RuleEngine = {
    // Define strict boundaries
    constraints: {
        maxIssuesPerDay: 50,
        forbiddenLabels: ['internal-only', 'security-vulnerability'],
        allowedRepos: ['joel4893/draft', 'joel4893/agentify']
    },

    /**
     * Validates a plan before execution.
     * @param {string} action - The intended action
     * @param {string} repo - The target repository
     * @param {Object} params - Action parameters
     * @returns {Object} { valid: boolean, reason: string|null }
     */
    validate(action, repo, params) {
        console.log(chalk.gray(`[RuleEngine] Validating ${action} on ${repo}...`));

        // Rule 1: Repository Authorization
        if (!this.constraints.allowedRepos.includes(repo)) {
            return { valid: false, reason: `Unauthorized repository: ${repo}` };
        }

        // Rule 2: Action-specific logic
        if (action === 'github.create_issue') {
            // Prevent empty or low-quality issues
            if (!params.title || params.title.length < 5) {
                return { valid: false, reason: 'Issue title is too short or missing.' };
            }

            // Prevent use of forbidden labels
            if (params.labels && params.labels.some(l => this.constraints.forbiddenLabels.includes(l))) {
                return { valid: false, reason: 'Plan contains forbidden labels.' };
            }
        }

        // Rule 3: Safety logic (e.g. "Do not create issues on weekends" or "Rate limits")
        // This is where you put your most critical business logic.

        return { valid: true, reason: null };
    }
};

module.exports = RuleEngine;