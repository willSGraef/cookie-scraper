// generateRules.js
// Run once with: node generateRules.js
// Generates a plain JSON rules file that Python can use directly
// without needing to inject any JS runtime into the page

const fs = require('fs');
const ac = require('@duckduckgo/autoconsent');

// decodeRules gives us the full list of CMP rules in their
// expanded form — each rule describes how to detect and interact
// with one CMP platform
const rules = ac.decodeRules(ac.filterCMPs());

const output = {
    generated: new Date().toISOString(),
    count: rules.length,
    cmps: rules.map(rule => ({
        name:        rule.name,
        // Detection: CSS selectors that identify this CMP is present
        detectCmp:   rule.detectCmp   || [],
        // Opt-in: steps to accept all cookies
        optIn:       rule.optIn       || [],
        // Opt-out: steps to reject non-essential cookies  
        optOut:      rule.optOut      || [],
        // Whether the CMP uses a hidden iframe for its UI
        intermediate: rule.intermediate || false,
    }))
};

fs.writeFileSync('lib/autoconsent-rules.json', JSON.stringify(output, null, 2));
console.log(`Written ${rules.length} CMP rules to lib/autoconsent-rules.json`);
console.log('CMP names:', rules.map(r => r.name).join(', '));