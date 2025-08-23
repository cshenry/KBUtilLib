# NotebookUtils Module

The `NotebookUtils` class provides utilities specifically designed for Jupyter notebook environments and interactive data analysis workflows.

## Overview

`NotebookUtils` extends `BaseUtils` to provide specialized functionality for working within Jupyter notebooks, including data visualization, interactive displays, progress tracking, and notebook-specific operations.

## Key Features

- **Enhanced DataFrame Display**: Interactive tables with pagination, search, and filtering
- **Interactive Visualizations**: Rich plotting and charting capabilities
- **Progress Tracking**: Real-time progress bars and status updates
- **Flexible Data Display**: Support for various data formats with automatic optimization
- **Export Utilities**: Save notebooks, figures, and data outputs
- **Widget Integration**: Support for interactive Jupyter widgets
- **Environment Detection**: Automatic detection of notebook vs. non-notebook environments

## Class Definition

```python
class NotebookUtils(BaseUtils):
    """Utilities for Jupyter notebook environments and interactive analysis.

    Provides methods for data visualization, interactive displays, progress
    tracking, and other notebook-specific functionality.
    """
```

## Constructor

```python
def __init__(self, notebook_folder: str, **kwargs: Any) -> None:
    """Initialize notebook utilities.

    Args:
        notebook_folder: Base directory for notebook operations
        **kwargs: Additional keyword arguments passed to BaseUtils
    """
```

## Data Display Methods

### Enhanced DataFrame Display

```python
def display_dataframe(
    self,
    df: Any,
    max_rows: Optional[int] = None,
    max_cols: Optional[int] = None,
    use_interactive: bool = True,
    page_size: int = 25,
    show_search: bool = True,
    show_info: bool = True,
    scrollable: bool = True,
    height: Optional[str] = None,
) -> None:
    """Display DataFrame with enhanced interactive features including pagination and search.
    
    Args:
        df: DataFrame to display (pandas, polars, etc.)
        max_rows: Maximum number of rows to display (None for all)
        max_cols: Maximum number of columns to display (None for all)
        use_interactive: Use interactive table with pagination and search
        page_size: Number of rows per page when using interactive display
        show_search: Show search functionality
        show_info: Show table information (row count, etc.)
        scrollable: Make table scrollable for large content
        height: Fixed height for the table (e.g., "400px")
    """
```

The enhanced `display_dataframe` method provides two clear display modes:

1. **Interactive Tables**: Uses `itables` for modern interactive display with pagination and search
2. **Sortable HTML**: Enhanced HTML tables with clickable column sorting (no dependencies needed)

#### Interactive Features:
- **Full Experience**: Install `itables` for pagination, search, and advanced table features
- **Basic Sorting**: Always available with clickable column headers and visual feedback
- **Clear Guidance**: Helpful messages guide users to install `itables` when needed
- **Smart Data Types**: Automatic numeric vs. text sorting in all modes

#### Features:
- **Search and Filter**: Real-time search across all columns
- **Pagination**: Configurable page sizes with navigation controls
- **Sorting**: Click column headers to sort data (even in HTML fallback mode)
- **Responsive**: Adapts to available screen space
- **Graceful Fallbacks**: Works even without optional dependencies
- **Smart Data Types**: Automatic numeric vs. text sorting
- **Visual Feedback**: Sort indicators, hover effects, and row highlighting

#### Example Usage:

```python
from kbutillib import NotebookUtils
import pandas as pd

# Initialize
nb_utils = NotebookUtils(notebook_folder=".")

# Create sample data
df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie', 'Diana'],
    'Age': [25, 30, 35, 28],
    'City': ['New York', 'London', 'Tokyo', 'Paris']
})

# Basic enhanced display
nb_utils.display_dataframe(df)

# Customized display
nb_utils.display_dataframe(
    df,
    page_size=10,
    height="300px",
    show_search=True
)

# Simple display without interactive features
nb_utils.display_dataframe(df, use_interactive=False)
```

### Other Display Methods

```python
def display_json(self, data: dict[str, Any], indent: int = 2) -> None:
    """Display JSON data with proper formatting in notebook."""

def display_markdown(self, text: str) -> None:
    """Display markdown text in notebook."""

def display_html(self, html: str) -> None:
    """Display HTML content in notebook."""
```

### Progress Tracking

```python
def create_progress_bar(self, total: int, description: str = "") -> Any:
    """Create a progress bar for long-running operations."""

def update_progress(self, progress_bar: Any, current: int) -> None:
    """Update progress bar with current value."""
```

## Visualization Methods

- `plot_line(data, **options)`: Create line plots with customization
- `plot_scatter(x, y, **options)`: Generate scatter plots
- `plot_histogram(data, **options)`: Create histogram visualizations
- `plot_heatmap(matrix, **options)`: Generate heatmap displays

### Advanced Visualizations

- `plot_network(nodes, edges, **options)`: Network diagram visualization
- `plot_tree(hierarchy, **options)`: Hierarchical tree displays
- `plot_3d_surface(data, **options)`: 3D surface plotting
- `animate_data(time_series, **options)`: Animated plot generation

### Biological Data Plots

- `plot_genome_browser(genome_data, **options)`: Genome visualization
- `plot_phylogenetic_tree(tree_data, **options)`: Phylogeny displays
- `plot_sequence_alignment(alignment, **options)`: Alignment visualization
- `plot_metabolic_network(network, **options)`: Metabolic pathway plots

## Interactive Features

### Progress Tracking

- `progress_bar(iterable, description)`: Enhanced progress bars
- `update_progress(current, total, message)`: Manual progress updates
- `status_display(status, color)`: Status indicator widgets
- `log_to_notebook(message, level)`: Interactive log display

### Data Exploration

- `interactive_dataframe(df)`: Enhanced DataFrame display
- `explore_data(dataset)`: Interactive data exploration widget
- `filter_widget(data, columns)`: Dynamic data filtering
- `summary_statistics(data)`: Interactive statistical summaries

### Input Widgets

- `text_input(prompt, default)`: Text input widgets
- `dropdown_select(options, prompt)`: Selection dropdowns
- `slider_input(min_val, max_val, step)`: Numerical sliders
- `file_upload_widget()`: File upload interface

## Export and Save Operations

### Figure Export

- `save_figure(figure, filename, format)`: Save plots in various formats
- `export_all_figures(directory)`: Batch export all generated figures
- `create_figure_gallery()`: Generate figure index/gallery
- `embed_figure_html(figure)`: Convert figures to HTML

### Data Export

- `save_dataframe(df, filename, format)`: Export DataFrames
- `export_notebook_data(directory)`: Save all notebook variables
- `create_data_package(metadata)`: Bundle data with metadata
- `generate_report(template)`: Create automated reports

### Notebook Operations

- `save_checkpoint(name)`: Create notebook checkpoint
- `export_notebook(format, filename)`: Export notebook to various formats
- `clean_notebook_outputs()`: Remove all cell outputs
- `extract_code_cells()`: Extract code to Python files

## Display Enhancements

### Rich Object Display

- `display_object(obj, **options)`: Enhanced object rendering
- `display_json(data, **options)`: Pretty JSON display
- `display_table(data, **options)`: Formatted table display
- `display_image(image, **options)`: Image display with options

### Scientific Data Display

- `display_sequence(sequence, **options)`: DNA/protein sequence display
- `display_structure(structure, **options)`: Molecular structure viewer
- `display_matrix(matrix, **options)`: Matrix visualization
- `display_tree_structure(tree, **options)`: Tree structure display

## Utility Functions

### Notebook Management

- `get_notebook_info()`: Current notebook metadata
- `list_variables()`: Display all notebook variables
- `memory_usage()`: Show memory consumption
- `execution_time(func)`: Measure and display execution time

### Environment Setup

- `setup_notebook_environment()`: Configure optimal notebook settings
- `install_extensions()`: Install useful Jupyter extensions
- `configure_matplotlib()`: Set up plotting backend
- `setup_widgets()`: Initialize interactive widgets

## Integration Features

### KBUtilLib Integration

- **BaseUtils Methods**: Inherits logging and configuration
- **Data Pipeline**: Seamless integration with other utility modules
- **Workflow Support**: Compatible with analysis workflows
- **Progress Reporting**: Unified progress tracking across modules

### External Library Support

- **Matplotlib/Seaborn**: Advanced plotting capabilities
- **Plotly**: Interactive web-based visualizations
- **IPython Widgets**: Rich interactive components
- **Pandas**: Enhanced DataFrame operations

## Configuration Options

### Display Settings

```python
notebook_utils = NotebookUtils(
    notebook_folder="/path/to/notebooks",
    figure_format="png",
    figure_dpi=300,
    max_display_rows=100,
    interactive_backend=True
)
```

### Visualization Defaults

- **Color Schemes**: Customizable color palettes
- **Font Settings**: Typography and sizing options
- **Layout Options**: Figure sizing and spacing
- **Export Quality**: Resolution and format settings

## Usage Examples

```python
from kbutillib.notebook_utils import NotebookUtils
import pandas as pd

# Initialize notebook utilities
nb = NotebookUtils(notebook_folder="./analysis")

# Create interactive visualization
data = pd.read_csv("experiment_data.csv")
nb.plot_scatter(
    data['x'], data['y'],
    color=data['category'],
    title="Experimental Results"
)

# Display progress during analysis
for i in nb.progress_bar(range(100), "Processing data"):
    # Perform analysis
    pass

# Save results
nb.save_figure(plt.gcf(), "results.png", format="png")
nb.save_dataframe(results, "analysis_output.csv")
```

## Best Practices

### Performance Optimization

- Use streaming for large datasets
- Limit plot complexity for responsiveness
- Cache computed visualizations
- Optimize memory usage for big data

### Notebook Organization

- Use consistent naming conventions
- Document analysis steps clearly
- Organize outputs in structured folders
- Maintain version control for notebooks

## Dependencies

- **jupyter**: Core Jupyter notebook environment
- **ipywidgets**: Interactive widget library
- **matplotlib**: Primary plotting library
- **seaborn**: Statistical visualization
- **plotly**: Interactive plotting (optional)
- **pandas**: Data manipulation and analysis
