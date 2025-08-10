# KBCallbackUtils Module

The `KBCallbackUtils` class provides utilities for managing KBase callback services and executing callback-based operations within the KBase SDK environment.

## Overview

`KBCallbackUtils` extends `SharedEnvUtils` to provide specialized functionality for handling KBase callback services, managing scratch directories, and facilitating communication between KBase services during SDK-based workflows.

## Key Features

- **Callback Service Management**: Start, stop, and manage callback services
- **Scratch Directory Handling**: Automatic scratch space management
- **Service Integration**: Seamless integration with KBase service ecosystem
- **Report Generation**: Enhanced report creation with callback support
- **File Management**: Efficient file operations in callback environments

## Class Definition

```python
class KBCallbackUtils(SharedEnvUtils):
    """Utilities enabling execution of KBase callbacks.

    Provides methods for managing callback services, scratch directories,
    and facilitating service-to-service communication in KBase workflows.
    """
```

## Constructor

```python
def __init__(
    self,
    callback_directory: Optional[Union[str, os.PathLike]] = "/tmp/scratch",
    callback_url: Optional[str] = None,
    **kwargs: Any
) -> None:
    """Initialize KBase callback utilities.

    Args:
        callback_directory: Directory for callback scratch operations
        callback_url: URL for callback service endpoint
        **kwargs: Additional keyword arguments passed to SharedEnvUtils
    """
```

## Core Methods

### Callback Service Management

- `start_callback_service()`: Initialize and start callback service
- `stop_callback_service()`: Gracefully stop callback service
- `get_callback_url()`: Retrieve active callback service URL
- `check_callback_status()`: Verify callback service health
- `restart_callback_service()`: Restart callback service if needed

### Directory Management

- `setup_scratch_directory()`: Initialize scratch workspace
- `cleanup_scratch_directory()`: Clean up temporary files
- `get_scratch_path(filename)`: Get full path to scratch file
- `list_scratch_contents()`: List files in scratch directory
- `archive_scratch_contents(archive_name)`: Archive scratch directory

### Service Client Methods

- `report_client()`: Get KBase Report service client
- `data_file_util_client()`: Get Data File Util service client
- `genome_file_util_client()`: Get Genome File Util service client
- `assembly_util_client()`: Get Assembly Util service client
- `rast_sdk_client()`: Get RAST SDK service client

### Report Generation

- `create_simple_report(message, **options)`: Generate basic reports
- `create_extended_report(data, **options)`: Create detailed reports
- `add_report_file(filepath, description)`: Add files to reports
- `save_report_to_kbase(report_data)`: Save reports to KBase
- `generate_html_report(template, data)`: Create HTML reports

## Advanced Features

### File Operations

- `upload_file_to_shock(filepath, **metadata)`: Upload files to Shock storage
- `download_file_from_shock(shock_id, target_path)`: Download Shock files
- `create_file_link(filepath, description)`: Create file references
- `validate_file_format(filepath, expected_format)`: Validate file types

### Data Processing

- `process_genome_file(filepath, **options)`: Process genome data files
- `process_assembly_file(filepath, **options)`: Handle assembly files
- `validate_input_data(data, schema)`: Validate input against schema
- `convert_file_format(input_path, output_format)`: Convert file formats

### Integration Methods

- `call_annotation_service(genome_ref, **params)`: Call annotation services
- `call_utility_module(method, params)`: Call utility module methods
- `submit_job_to_execution_engine(job_params)`: Submit asynchronous jobs
- `monitor_job_progress(job_id)`: Track job execution status

## Workflow Support

### SDK Integration

The module provides seamless integration with KBase SDK workflows:

```python
from kbutillib.kb_callback_utils import KBCallbackUtils

# Initialize in SDK environment
callback_utils = KBCallbackUtils(
    callback_directory="/kb/module/work/tmp",
    callback_url=os.environ.get('SDK_CALLBACK_URL')
)

# Start callback service
callback_utils.start_callback_service()

# Perform operations with callback support
report = callback_utils.create_extended_report({
    'text_message': 'Analysis completed successfully',
    'warnings': [],
    'objects_created': []
})
```

### Service Communication

- **Asynchronous Operations**: Support for long-running processes
- **Progress Reporting**: Real-time progress updates
- **Error Handling**: Comprehensive error reporting and recovery
- **Resource Management**: Automatic cleanup of temporary resources

## Configuration

### Environment Variables

The module recognizes several environment variables:

- `SDK_CALLBACK_URL`: Callback service endpoint
- `KB_AUTH_TOKEN`: Authentication token
- `KBASE_ENDPOINT`: KBase service endpoint
- `SCRATCH_DIR`: Scratch directory location

### Service Configuration

```python
# Configure callback service settings
callback_utils.configure_service({
    'timeout': 300,
    'max_retries': 3,
    'cleanup_on_exit': True,
    'log_level': 'INFO'
})
```

## Error Handling

### Common Error Scenarios

- **Service Unavailable**: Automatic retry with exponential backoff
- **Authentication Failures**: Clear error messages and resolution steps
- **Resource Exhaustion**: Graceful degradation and cleanup
- **Network Issues**: Robust retry mechanisms

### Error Recovery

- `recover_from_callback_failure()`: Attempt service recovery
- `cleanup_failed_operations()`: Clean up partial operations
- `generate_error_report(error_info)`: Create detailed error reports
- `log_callback_errors(error_details)`: Comprehensive error logging

## Security Considerations

### Authentication

- **Token Management**: Secure handling of authentication tokens
- **Service Verification**: Validation of service endpoints
- **Access Control**: Proper permission checking
- **Audit Logging**: Complete operation logging

### Data Protection

- **Temporary File Security**: Secure handling of temporary files
- **Data Encryption**: Optional encryption for sensitive data
- **Access Restrictions**: Proper file permission management
- **Cleanup Verification**: Secure deletion of temporary data

## Performance Optimization

### Resource Management

- **Memory Efficiency**: Optimized memory usage for large files
- **Disk Space**: Automatic cleanup of temporary files
- **Network Optimization**: Efficient data transfer protocols
- **Caching**: Intelligent caching of frequently accessed data

### Monitoring

- `get_resource_usage()`: Monitor CPU and memory usage
- `track_operation_performance()`: Performance metrics collection
- `optimize_callback_settings()`: Auto-tune performance settings
- `generate_performance_report()`: Performance analysis reports

## Dependencies

- **requests**: HTTP communication with KBase services
- **os**: Operating system interface operations
- **pathlib**: Modern path handling
- **uuid**: Unique identifier generation
- **json**: Data serialization for service communication

## Integration Examples

```python
# Complete workflow example
from kbutillib.kb_callback_utils import KBCallbackUtils

class MyKBaseApp(KBCallbackUtils):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def run_analysis(self, input_params):
        # Setup callback environment
        self.start_callback_service()

        try:
            # Process input data
            genome_ref = input_params['genome_ref']

            # Call annotation service
            annotation_result = self.call_annotation_service(
                genome_ref,
                method='annotate_genome'
            )

            # Generate report
            report = self.create_extended_report({
                'text_message': f'Annotated {len(annotation_result["features"])} features',
                'objects_created': [annotation_result['annotation_ref']]
            })

            return report

        finally:
            # Cleanup
            self.stop_callback_service()
            self.cleanup_scratch_directory()
```
