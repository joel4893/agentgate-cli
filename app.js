const { trace } = require('./trace');

async function agentAction() {
    console.log("Agent is analyzing the repository context...");

    // Autonomous request from the Agent
    const result = await trace.call("github.create_issue", {
        title: "Autonomous Issue from Agentgate",
        body: "This issue was logged and controlled by the Agentgate middle-layer."
    });

    console.log("Agentgate Response:", result);
}
agentAction();