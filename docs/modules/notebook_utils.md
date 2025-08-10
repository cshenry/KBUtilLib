# NotebookUtils Module

The `NotebookUtils` class provides utilities specifically designed for Jupyter notebook environments and interactive data analysis workflows.

## Overview

`NotebookUtils` extends `BaseUtils` to provide specialized functionality for working within Jupyter notebooks, including data visualization, interactive displays, progress tracking, and notebook-specific operations.

## Key Features

- **Interactive Visualizations**: Rich plotting and charting capabilities
- **Progress Tracking**: Real-time progress bars and status updates
- **Data Display**: Enhanced data frame and object rendering
- **Export Utilities**: Save notebooks, figures, and data outputs
- **Widget Integration**: Support for interactive Jupyter widgets

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

## Visualization Methods

### Basic Plotting

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
