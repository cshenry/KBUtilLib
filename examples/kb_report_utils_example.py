"""Example usage of KBReportUtils for creating HTML reports.

This example demonstrates how to use the KBReportUtils class to create
interactive HTML reports that can communicate with KBase services.
"""

from kbutillib import KBReportUtils


def example_basic_report():
    """Example: Create a basic HTML report."""
    print("Example 1: Basic HTML Report")
    print("-" * 50)

    report_utils = KBReportUtils()

    # Create a simple HTML report
    html = report_utils.create_html_report(
        title="My Analysis Report",
        content="""
        <h2>Analysis Results</h2>
        <p>This is a simple report with basic content.</p>
        <ul>
            <li>Result 1: Successfully processed 100 samples</li>
            <li>Result 2: Found 25 significant genes</li>
            <li>Result 3: Generated 5 plots</li>
        </ul>
        """,
        include_kbase_js=False,
        include_bootstrap=True,
    )

    # Save to file for viewing
    with open("/tmp/basic_report.html", "w") as f:
        f.write(html)

    print("✓ Basic report saved to /tmp/basic_report.html")
    print()


def example_table_report():
    """Example: Create a table report from data."""
    print("Example 2: Table Report")
    print("-" * 50)

    report_utils = KBReportUtils()

    # Sample data
    table_data = [
        {"Gene ID": "gene_001", "Expression": 15.3, "P-value": 0.001},
        {"Gene ID": "gene_002", "Expression": 12.7, "P-value": 0.005},
        {"Gene ID": "gene_003", "Expression": 8.9, "P-value": 0.012},
        {"Gene ID": "gene_004", "Expression": 6.2, "P-value": 0.023},
    ]

    html = report_utils.create_simple_table_report(
        title="Gene Expression Analysis",
        table_data=table_data,
        description="Top differentially expressed genes from RNA-seq analysis",
    )

    # Save to file
    with open("/tmp/table_report.html", "w") as f:
        f.write(html)

    print("✓ Table report saved to /tmp/table_report.html")
    print()


def example_interactive_report():
    """Example: Create an interactive report with multiple sections."""
    print("Example 3: Interactive Multi-Section Report")
    print("-" * 50)

    report_utils = KBReportUtils()

    # Define multiple sections
    sections = [
        {
            "title": "Overview",
            "content": """
            <h3>Analysis Overview</h3>
            <p>This analysis processed genomic data from 3 conditions.</p>
            <ul>
                <li>Control: 30 samples</li>
                <li>Treatment A: 30 samples</li>
                <li>Treatment B: 30 samples</li>
            </ul>
            """,
        },
        {
            "title": "Results",
            "content": """
            <h3>Key Findings</h3>
            <div class="alert alert-info">
                <strong>Important:</strong> Treatment B showed significant effects.
            </div>
            <p>We identified 152 differentially expressed genes.</p>
            """,
        },
        {
            "title": "Statistics",
            "content": """
            <h3>Statistical Summary</h3>
            <table class="table table-striped">
                <tr><th>Metric</th><th>Value</th></tr>
                <tr><td>Total Genes</td><td>20,000</td></tr>
                <tr><td>Significant Genes</td><td>152</td></tr>
                <tr><td>FDR Threshold</td><td>0.05</td></tr>
            </table>
            """,
        },
    ]

    html = report_utils.create_interactive_report(
        title="Genomic Analysis Report",
        sections=sections,
        workspace_ref="12345/1/1",  # Optional workspace reference
    )

    # Save to file
    with open("/tmp/interactive_report.html", "w") as f:
        f.write(html)

    print("✓ Interactive report saved to /tmp/interactive_report.html")
    print()


def example_kbase_enabled_report():
    """Example: Create a report with KBase API integration."""
    print("Example 4: KBase-Enabled Report")
    print("-" * 50)

    report_utils = KBReportUtils()

    # Create report with embedded JavaScript for KBase API calls
    content = """
    <h2>KBase Data Viewer</h2>
    <p>This report includes JavaScript code to interact with KBase services.</p>

    <div id="auth-status"></div>
    <button class="btn btn-primary" onclick="loadWorkspaceData()">Load Workspace Data</button>
    <div id="data-container" style="margin-top: 20px;"></div>
    """

    # Custom JavaScript that uses the KBase utilities
    custom_js = """
    // Check authentication status
    document.addEventListener('DOMContentLoaded', function() {
        const authDiv = document.getElementById('auth-status');
        if (window.kbReportUtils.isAuthenticated()) {
            authDiv.innerHTML = '<div class="alert alert-success">✓ Authenticated with KBase</div>';
        } else {
            authDiv.innerHTML = '<div class="alert alert-warning">⚠ Not authenticated. Please log in to KBase.</div>';
        }
    });

    // Function to load workspace data
    async function loadWorkspaceData() {
        const container = document.getElementById('data-container');
        container.innerHTML = '<p>Loading...</p>';

        try {
            // Example: Get workspace info (requires authentication)
            // const wsInfo = await window.kbReportUtils.getWorkspaceInfo(12345);
            // container.innerHTML = '<pre>' + JSON.stringify(wsInfo, null, 2) + '</pre>';

            // For demo purposes without real workspace
            container.innerHTML = `
                <div class="alert alert-info">
                    <strong>Demo Mode:</strong> In production, this would load data from workspace.
                    <br>Example usage: <code>await kbReportUtils.getObject('12345/1/1')</code>
                </div>
            `;
        } catch (error) {
            container.innerHTML = '<div class="alert alert-danger">Error: ' + error.message + '</div>';
        }
    }
    """

    html = report_utils.create_html_report(
        title="KBase-Enabled Report",
        content=content,
        include_kbase_js=True,  # Include KBase JavaScript utilities
        custom_js=custom_js,
        include_bootstrap=True,
    )

    # Save to file
    with open("/tmp/kbase_enabled_report.html", "w") as f:
        f.write(html)

    print("✓ KBase-enabled report saved to /tmp/kbase_enabled_report.html")
    print("  Open this in a browser with KBase session cookies to test API features")
    print()


def example_save_to_workspace():
    """Example: Save a report to KBase workspace (requires authentication)."""
    print("Example 5: Save Report to Workspace")
    print("-" * 50)
    print("NOTE: This example requires valid KBase credentials and workspace access")
    print()

    # Commented out to avoid authentication errors in example
    """
    report_utils = KBReportUtils(kb_version='prod')

    # Create report
    html = report_utils.create_html_report(
        title="Analysis Report",
        content="<h2>Results</h2><p>Analysis complete!</p>"
    )

    # Save to workspace
    result = report_utils.save_html_report(
        html_content=html,
        report_name="my_analysis_report",
        workspace_id=12345,  # Your workspace ID
        report_params={
            "text_message": "Analysis completed successfully",
            "warnings": [],
            "objects_created": [
                {"ref": "12345/2/1", "description": "Output model"}
            ]
        }
    )

    print(f"✓ Report saved: {result['report_ref']}")
    """

    print("Example code is commented out - requires authentication")
    print()


def main():
    """Run all examples."""
    print("\n" + "=" * 50)
    print("KBReportUtils Examples")
    print("=" * 50 + "\n")

    example_basic_report()
    example_table_report()
    example_interactive_report()
    example_kbase_enabled_report()
    example_save_to_workspace()

    print("=" * 50)
    print("All examples completed!")
    print("=" * 50)


if __name__ == "__main__":
    main()
