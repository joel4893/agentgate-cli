const fs = require('fs-extra');
const path = require('path');
const axios = require('axios');

require('dotenv').config({ path: path.resolve(__dirname, '.env') }); // Load .env file


const trace = {
    call: async (action, params) => {
        // Get context from the local wrap
        const contextPath = path.join(process.cwd(), '.agentgate', 'context.json');
        if (!fs.existsSync(contextPath)) {
            throw new Error("Local context not found. Run 'agentgate wrap' first.");
        }
        const context = fs.readJsonSync(contextPath);

        // The SDK now points to the Agentgate Backend Service
        const AGENTGATE_SERVER = process.env.AGENTGATE_API_URL || "http://localhost:3000/v1/dispatch";

        try {
            const response = await axios.post(AGENTGATE_SERVER, {
                action,
                repository: context.repoIdentifier,
                parameters: params
            });

            return response.data;
        } catch (error) {
            // Controllable: The SDK can handle specific error types from the server
            if (error.code === 'ECONNREFUSED') {
                throw new Error("Agentgate Backend is offline. Reliability check failed.");
            }
            throw new Error(
                `Agentgate Error: ${error.response?.data?.error || error.message}`
            );
        }
    }
};

module.exports = { trace };