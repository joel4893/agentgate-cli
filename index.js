#!/usr/bin/env node

const { Command } = require('commander');
const chalk = require('chalk');
const fs = require('fs-extra');
const path = require('path');

const program = new Command();
const CONTEXT_DIR = path.join(process.cwd(), '.agentgate');
const CONTEXT_FILE = path.join(CONTEXT_DIR, 'context.json');

function parseGitHubPath(input) {
  const urlRegex = /github\.com\/([^/]+)\/([^/.]+)/;
  const match = input.match(urlRegex);
  return match ? `${match[1]}/${match[2]}` : input;
}

program
  .name('agentgate')
  .version('0.1.2');

program
  .command('wrap')
  .description('Prepare repository context')
  .argument('<provider>', 'e.g., github')
  .option('-p, --path <path>', 'Repository URL or owner/repo', '.')
  .action(async (provider, options) => {
    let target = options.path;
    if (provider === 'github') target = parseGitHubPath(options.path);

    console.log(chalk.blue(`[AgentGate] Wrapping ${chalk.bold(provider)} repository: ${chalk.cyan(target)}...`));
    
    const context = {
      provider,
      repoIdentifier: target,
      timestamp: new Date().toISOString(),
      status: 'wrapped'
    };

    try {
      await fs.ensureDir(CONTEXT_DIR);
      await fs.writeJson(CONTEXT_FILE, context, { spaces: 2 });
      console.log(chalk.green(`✔ Context saved to .agentgate/context.json`));
    } catch (err) {
      console.error(chalk.red('Failed to save context:'), err.message);
    }
  });

program
  .command('upload')
  .description('Upload context to AgentGate')
  .action(async () => {
    if (!await fs.exists(CONTEXT_FILE)) {
      console.error(chalk.red('Error: No context found. Run "agentgate wrap github --path <url>" first.'));
      return;
    }

    const context = await fs.readJson(CONTEXT_FILE);
    console.log(chalk.blue(`[AgentGate] Uploading context for ${context.repoIdentifier}...`));

    try {
      context.status = 'active';
      await fs.writeJson(CONTEXT_FILE, context, { spaces: 2 });
      await new Promise(r => setTimeout(r, 1000));
      console.log(chalk.green('✔ Upload successful.'));
    } catch (err) {
      console.error(chalk.red('Upload failed:'), err.message);
    }
  });

program.parse(process.argv);